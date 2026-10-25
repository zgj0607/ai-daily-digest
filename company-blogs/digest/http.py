from __future__ import annotations

import re
import threading
import time
from urllib.parse import urljoin

# OpenRouter 等站点对短链接返回 200 + meta refresh，而不是 HTTP 重定向。
_META_REFRESH = re.compile(
    r"""<meta\b[^>]*http-equiv=["']refresh["'][^>]*content=["'][^"']*?url=([^"'\s>]+)""",
    re.I,
)
_META_REFRESH_REV = re.compile(
    r"""<meta\b[^>]*content=["'][^"']*?url=([^"'\s>]+)[^"']*["'][^>]*http-equiv=["']refresh["']""",
    re.I,
)


class Fetcher:
    """全局限速 + 每线程独立 Session。请求使用浏览器指纹，避免站点按爬虫拦截。"""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self.timeout = (15, 40)
        self._last_request = 0.0
        self._rate_lock = threading.Lock()
        self._local = threading.local()
        self._sessions: list = []
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            from curl_cffi import requests as browser_requests

            session = browser_requests.Session(impersonate="chrome")
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _rate_limit(self) -> None:
        """只在限速时持锁；网络 IO 在锁外进行。"""
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request
            if self._last_request and elapsed < self.interval_seconds:
                time.sleep(self.interval_seconds - elapsed)
            self._last_request = time.monotonic()

    def get(self, url: str):
        last_error: Exception | None = None
        for attempt in range(3):
            self._rate_limit()
            try:
                response = self._session().get(url, timeout=self.timeout, allow_redirects=True)
            except OSError as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(2 ** attempt)
                continue
            try:
                response.raise_for_status()
                return response
            except OSError as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"抓取失败 {url}: {last_error}")

    def text(self, url: str) -> str:
        html, _final = self.text_resolved(url, hops=0)
        return html

    def text_resolved(self, url: str, hops: int = 3) -> tuple[str, str]:
        """返回 (HTML, 最终地址)。会继续打开 HTML 里的 meta refresh。"""
        current = url
        html = ""
        for _ in range(hops + 1):
            response = self.get(current)
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = getattr(response, "apparent_encoding", None) or "utf-8"
            html = response.text
            target = _html_redirect(html, response.url or current)
            if not target or target.rstrip("/") == current.rstrip("/"):
                return html, response.url or current
            current = target
        return html, current

    def bytes(self, url: str) -> bytes:
        return self.get(url).content


def _html_redirect(html: str, base_url: str) -> str:
    if len(html) > 20000 or "redirecting" not in html[:4000].lower():
        return ""
    match = _META_REFRESH.search(html) or _META_REFRESH_REV.search(html)
    if not match:
        return ""
    return urljoin(base_url, match.group(1).strip())

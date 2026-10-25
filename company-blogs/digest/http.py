from __future__ import annotations

import re
import threading
import time
from urllib.parse import urljoin, urlparse

# OpenRouter 等站点对短链接返回 200 + meta refresh，而不是 HTTP 重定向。
_META_REFRESH = re.compile(
    r"""<meta\b[^>]*http-equiv=["']refresh["'][^>]*content=["'][^"']*?url=([^"'\s>]+)""",
    re.I,
)
_META_REFRESH_REV = re.compile(
    r"""<meta\b[^>]*content=["'][^"']*?url=([^"'\s>]+)[^"']*["'][^>]*http-equiv=["']refresh["']""",
    re.I,
)
# 浮动的 chrome 别名目前会被 Sequoia 的 Cloudflare 直接 403。按实测稳定的版本依次尝试。
_BROWSER_PROFILES = ("chrome124", "safari184", "chrome120")


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
        self._serial_hosts: set[str] = set()
        self._host_locks: dict[str, threading.Lock] = {}
        self._serial_guard = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            from curl_cffi import requests as browser_requests

            profile = _BROWSER_PROFILES[getattr(self._local, "profile", 0) % len(_BROWSER_PROFILES)]
            session = browser_requests.Session(impersonate=profile)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _drop_session(self) -> None:
        current = getattr(self._local, "session", None)
        self._local.session = None
        if current is None:
            return
        current.close()
        with self._sessions_lock:
            if current in self._sessions:
                self._sessions.remove(current)

    def _rotate_browser(self) -> None:
        """当前指纹被 Cloudflare 拦住时换下一个，并丢掉这次 403 留下的 cookie。"""
        self._drop_session()
        self._local.profile = getattr(self._local, "profile", 0) + 1

    def _reset_browser(self) -> None:
        self._drop_session()
        self._local.profile = 0

    def _rate_limit(self) -> None:
        """只在限速时持锁；网络 IO 在锁外进行。"""
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request
            if self._last_request and elapsed < self.interval_seconds:
                time.sleep(self.interval_seconds - elapsed)
            self._last_request = time.monotonic()

    def _host(self, url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def _is_serial(self, host: str) -> bool:
        with self._serial_guard:
            return host in self._serial_hosts

    def _mark_serial(self, host: str) -> None:
        with self._serial_guard:
            self._serial_hosts.add(host)
            self._host_locks.setdefault(host, threading.Lock())

    def _host_lock(self, host: str) -> threading.Lock:
        with self._serial_guard:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def get(self, url: str):
        host = self._host(url)
        if self._is_serial(host):
            with self._host_lock(host):
                return self._get(url, serial=True)
        try:
            return self._get(url, serial=False)
        except _CloudflareBlock:
            # 并行请求会触发整站 403。改成这个域名一次只发一个请求再试。
            self._mark_serial(host)
            with self._host_lock(host):
                return self._get(url, serial=True)

    def _get(self, url: str, serial: bool):
        last_error: Exception = RuntimeError("没有响应")
        attempts = len(_BROWSER_PROFILES) if serial else 1
        for attempt in range(attempts):
            self._rate_limit()
            try:
                response = self._session().get(url, timeout=self.timeout, allow_redirects=True)
            except OSError as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 403 and _cloudflare_blocked(response):
                last_error = RuntimeError("HTTP 403")
                if not serial:
                    self._reset_browser()
                    raise _CloudflareBlock()
                self._rotate_browser()
                time.sleep(1 + attempt)
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


class _CloudflareBlock(Exception):
    pass


def _cloudflare_blocked(response) -> bool:
    server = (response.headers.get("server") or "").lower()
    if "cloudflare" not in server and "cf-ray" not in response.headers:
        return False
    snippet = response.text[:2000].lower()
    return "attention required" in snippet or "just a moment" in snippet


def _html_redirect(html: str, base_url: str) -> str:
    if len(html) > 20000 or "redirecting" not in html[:4000].lower():
        return ""
    match = _META_REFRESH.search(html) or _META_REFRESH_REV.search(html)
    if not match:
        return ""
    return urljoin(base_url, match.group(1).strip())

from __future__ import annotations

import threading
import time

import requests


class Fetcher:
    def __init__(self, user_agent: str, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        self.timeout = (15, 40)

    def close(self) -> None:
        self.session.close()

    def get(self, url: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            with self._lock:
                self._pause_unlocked()
                try:
                    response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                except requests.RequestException as exc:
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
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"抓取失败 {url}: {last_error}")

    def text(self, url: str) -> str:
        response = self.get(url)
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def bytes(self, url: str) -> bytes:
        return self.get(url).content

    def _pause_unlocked(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.interval_seconds:
            time.sleep(self.interval_seconds - elapsed)
        self._last_request = time.monotonic()

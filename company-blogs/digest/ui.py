"""终端面板：整体进展固定，爬取和翻译各自在框内滚动。"""
from __future__ import annotations

import shutil
import sys
import threading
from collections import deque


def _char_width(ch: str) -> int:
    code = ord(ch)
    if (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or code >= 0x1F300
    ):
        return 2
    return 1


def _text_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    out: list[str] = []
    for ch in text:
        step = _char_width(ch)
        if used + step > width:
            break
        out.append(ch)
        used += step
    if used < _text_width(text) and width >= 1:
        while out and used + 1 > width:
            used -= _char_width(out.pop())
        if used + 1 <= width:
            out.append("…")
            used += 1
    return "".join(out) + (" " * (width - used))


def _box(title: str, rows: list[str], width: int, height: int) -> list[str]:
    inner_w = max(1, width - 2)
    inner_h = max(1, height - 2)
    label = f" {title} "
    if _text_width(label) > inner_w:
        label = _fit(label, inner_w)
    pad = inner_w - _text_width(label)
    top = "┌" + label + ("─" * pad) + "┐"
    body = ["│" + _fit(line, inner_w) + "│" for line in rows[:inner_h]]
    while len(body) < inner_h:
        body.append("│" + (" " * inner_w) + "│")
    bottom = "└" + ("─" * inner_w) + "┘"
    return [top, *body, bottom]


class Dashboard:
    def __init__(self) -> None:
        self.live = False
        self.header = ["准备中"]
        self.crawl: deque[str] = deque(maxlen=400)
        self.translate: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()

    def start(self) -> None:
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        tty = sys.stdout.isatty()
        with self._lock:
            self.live = bool(tty and cols >= 48 and rows >= 12)
            if not self.live:
                return
            sys.stdout.write("\033[?1049h\033[?25l")
            self._render()

    def stop(self) -> None:
        with self._lock:
            if not self.live:
                return
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            self.live = False

    def set_header(self, lines: list[str]) -> None:
        with self._lock:
            self.header = [line for line in lines if line]
            self._render()

    def log_crawl(self, text: str) -> None:
        self._log(self.crawl, text)

    def log_translate(self, text: str) -> None:
        self._log(self.translate, text)

    def _log(self, bucket: deque[str], text: str) -> None:
        with self._lock:
            if not self.live:
                print(text, flush=True)
                return
            bucket.append(text)
            self._render()

    def _render(self) -> None:
        if not self.live:
            return
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        cols = max(48, cols)
        rows = max(12, rows)
        header_h = min(8, 2 + max(1, len(self.header)))
        pane_h = rows - header_h
        if pane_h < 5:
            header_h = max(4, rows - 5)
            pane_h = rows - header_h
        header = _box("整体进展", self.header, cols, header_h)
        side_by_side = cols >= 72 and pane_h >= 6
        if side_by_side:
            gap = 1
            left_w = (cols - gap) // 2
            right_w = cols - gap - left_w
            inner = pane_h - 2
            left = _box("爬取", list(self.crawl)[-inner:], left_w, pane_h)
            right = _box("翻译", list(self.translate)[-inner:], right_w, pane_h)
            panes = [a + (" " * gap) + b for a, b in zip(left, right)]
        else:
            top_h = pane_h // 2
            bottom_h = pane_h - top_h
            panes = [
                *_box("爬取", list(self.crawl)[-(top_h - 2):], cols, top_h),
                *_box("翻译", list(self.translate)[-(bottom_h - 2):], cols, bottom_h),
            ]
        lines = (header + panes)[:rows]
        sys.stdout.write("\033[H" + "\n".join(lines) + "\033[J")
        sys.stdout.flush()


board = Dashboard()

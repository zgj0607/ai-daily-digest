"""终端面板：日志按宽度换行，框内可翻阅历史。"""
from __future__ import annotations

import atexit
import os
import select
import shutil
import sys
import termios
import threading
import time
import tty
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


def _wrap_text(text: str, width: int) -> list[str]:
    """按显示宽度折行，保留原文里的换行，不截断。"""
    width = max(1, width)
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n") or [""]:
        if raw == "":
            lines.append("")
            continue
        buf: list[str] = []
        used = 0
        for ch in raw:
            step = _char_width(ch)
            if buf and used + step > width:
                lines.append("".join(buf))
                buf = []
                used = 0
            buf.append(ch)
            used += step
        if buf:
            lines.append("".join(buf))
    return lines or [""]


def _pad(text: str, width: int) -> str:
    gap = width - _text_width(text)
    if gap <= 0:
        return _fit(text, width)
    return text + (" " * gap)


def _box(title: str, rows: list[str], width: int, height: int) -> list[str]:
    inner_w = max(1, width - 2)
    inner_h = max(1, height - 2)
    label = f" {title} "
    if _text_width(label) > inner_w:
        label = _fit(label, inner_w)
    pad = inner_w - _text_width(label)
    top = "┌" + label + ("─" * pad) + "┐"
    body = ["│" + _pad(line, inner_w) + "│" for line in rows[:inner_h]]
    while len(body) < inner_h:
        body.append("│" + (" " * inner_w) + "│")
    bottom = "└" + ("─" * inner_w) + "┘"
    return [top, *body, bottom]


class _Pane:
    def __init__(self) -> None:
        self.records: deque[str] = deque(maxlen=800)
        self.anchor = 0
        self.follow = True
        self.total = 0
        self.page = 1
        self.top = 1
        self.bottom = 1
        self.left = 1
        self.right = 1


class Dashboard:
    def __init__(self) -> None:
        self.live = False
        self.header = ["准备中"]
        self.crawl = _Pane()
        self.translate = _Pane()
        self._focus = "translate"
        self._lock = threading.Lock()
        self._last_paint = 0.0
        self._timer: threading.Timer | None = None
        self._input_thread: threading.Thread | None = None
        self._term_fd: int | None = None
        self._term_attr: list | None = None
        self._inbuf = b""

    def start(self) -> None:
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        tty = sys.stdout.isatty()
        with self._lock:
            self.live = bool(tty and cols >= 48 and rows >= 12)
            if not self.live:
                return
            sys.stdout.write("\033[?1049h\033[?25l")
            self._enable_input()
            self._render()

    def stop(self) -> None:
        thread: threading.Thread | None = None
        with self._lock:
            timer = self._timer
            self._timer = None
            if timer:
                timer.cancel()
            if self.live:
                self._render()
                sys.stdout.write("\033[?25h\033[?1000l\033[?1006l\033[?1049l")
                sys.stdout.flush()
                self.live = False
            thread = self._input_thread
            self._input_thread = None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.6)
        self._restore_term()

    def set_header(self, lines: list[str]) -> None:
        with self._lock:
            self.header = [line for line in lines if line]
            self._render()

    def log_crawl(self, text: str) -> None:
        self._log(self.crawl, text)

    def log_translate(self, text: str) -> None:
        self._log(self.translate, text)

    def _log(self, pane: _Pane, text: str) -> None:
        with self._lock:
            if not self.live:
                print(text, flush=True)
                return
            pane.records.append(text)
            self._paint(coalesce=True)

    def _paint(self, coalesce: bool) -> None:
        now = time.monotonic()
        if not coalesce or now - self._last_paint >= 0.08:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._last_paint = now
            self._render()
            return
        if self._timer:
            return
        timer = threading.Timer(0.08, self._flush)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            self._last_paint = time.monotonic()
            self._render()

    def _enable_input(self) -> None:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        self._term_fd = fd
        self._term_attr = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        thread = threading.Thread(target=self._input_loop, name="dashboard-keys", daemon=True)
        self._input_thread = thread
        thread.start()

    def _restore_term(self) -> None:
        fd = self._term_fd
        attr = self._term_attr
        self._term_fd = None
        self._term_attr = None
        if fd is None or attr is None:
            return
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, attr)
        except termios.error:
            pass

    def _input_loop(self) -> None:
        fd = self._term_fd
        if fd is None:
            return
        while True:
            with self._lock:
                if not self.live:
                    return
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(fd, 128)
            except OSError:
                return
            if not chunk:
                return
            self._inbuf += chunk
            self._drain_input()

    def _drain_input(self) -> None:
        while self._inbuf:
            if self._inbuf.startswith(b"\x1b[<"):
                end = -1
                for index, byte in enumerate(self._inbuf[3:], start=3):
                    if byte in (ord("M"), ord("m")):
                        end = index
                        break
                if end < 0:
                    return
                packet = self._inbuf[3:end]
                release = self._inbuf[end:end + 1] == b"m"
                self._inbuf = self._inbuf[end + 1:]
                self._mouse(packet.decode("ascii", "ignore"), release)
                continue
            if self._inbuf.startswith(b"\x1b"):
                if len(self._inbuf) == 1:
                    return
                if self._inbuf[1:2] != b"[":
                    self._inbuf = self._inbuf[1:]
                    continue
                end = -1
                for index, byte in enumerate(self._inbuf[2:], start=2):
                    if 0x40 <= byte <= 0x7E:
                        end = index
                        break
                if end < 0:
                    return
                seq = self._inbuf[: end + 1]
                self._inbuf = self._inbuf[end + 1:]
                self._key(seq)
                continue
            byte = self._inbuf[:1]
            self._inbuf = self._inbuf[1:]
            if byte == b"\t":
                self._switch_focus()

    def _key(self, seq: bytes) -> None:
        if seq in (b"\x1b[A", b"\x1b[1;2A"):
            self._nudge(self._focus, 1)
        elif seq in (b"\x1b[B", b"\x1b[1;2B"):
            self._nudge(self._focus, -1)
        elif seq == b"\x1b[5~":
            self._nudge(self._focus, self._page(self._focus))
        elif seq == b"\x1b[6~":
            self._nudge(self._focus, -self._page(self._focus))
        elif seq in (b"\x1b[H", b"\x1b[1~", b"\x1b[7~"):
            self._nudge(self._focus, 10**6)
        elif seq in (b"\x1b[F", b"\x1b[4~", b"\x1b[8~"):
            self._jump_latest(self._focus)

    def _mouse(self, packet: str, release: bool) -> None:
        if release:
            return
        parts = packet.split(";")
        if len(parts) != 3:
            return
        try:
            button = int(parts[0])
            col = int(parts[1])
            row = int(parts[2])
        except ValueError:
            return
        pane = self._pane_at(col, row)
        if pane is None:
            return
        code = button & 0x7F
        if code == 64:
            self._nudge(pane, 3)
        elif code == 65:
            self._nudge(pane, -3)

    def _pane_at(self, col: int, row: int) -> str | None:
        for name in ("crawl", "translate"):
            pane = self.crawl if name == "crawl" else self.translate
            if pane.top <= row <= pane.bottom and pane.left <= col <= pane.right:
                return name
        return None

    def _page(self, name: str) -> int:
        pane = self.crawl if name == "crawl" else self.translate
        return max(1, pane.page)

    def _switch_focus(self) -> None:
        with self._lock:
            if not self.live:
                return
            self._focus = "crawl" if self._focus == "translate" else "translate"
            self._render()

    def _jump_latest(self, name: str) -> None:
        with self._lock:
            if not self.live:
                return
            pane = self.crawl if name == "crawl" else self.translate
            pane.follow = True
            self._focus = name
            self._render()

    def _nudge(self, name: str, delta: int) -> None:
        """delta > 0 往更早的记录翻，delta < 0 往最新翻。"""
        with self._lock:
            if not self.live:
                return
            pane = self.crawl if name == "crawl" else self.translate
            latest = max(0, pane.total - pane.page)
            pane.follow = False
            pane.anchor = min(latest, max(0, pane.anchor - delta))
            if pane.anchor >= latest:
                pane.follow = True
            self._focus = name
            self._render()

    def _render(self) -> None:
        if not self.live:
            return
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        cols = max(48, cols)
        rows = max(12, rows)
        header_h = min(8, 2 + max(1, len(self.header)))
        pane_h = rows - header_h
        if pane_h < 8:
            header_h = max(4, rows - 8)
            pane_h = rows - header_h
        translate_h = max(5, int(pane_h * 0.62))
        crawl_h = pane_h - translate_h
        if crawl_h < 5:
            crawl_h = min(5, max(4, pane_h // 3))
            translate_h = pane_h - crawl_h
        header = _box("整体进展", self.header, cols, header_h)
        crawl_box = self._pane_box("爬取", self.crawl, "crawl", cols, crawl_h)
        translate_box = self._pane_box("翻译", self.translate, "translate", cols, translate_h)
        self.crawl.top = header_h + 1
        self.crawl.bottom = header_h + crawl_h
        self.translate.top = header_h + crawl_h + 1
        self.translate.bottom = header_h + crawl_h + translate_h
        for pane in (self.crawl, self.translate):
            pane.left = 1
            pane.right = cols
        lines = (header + crawl_box + translate_box)[:rows]
        sys.stdout.write("\033[H" + "\n".join(lines) + "\033[J")
        sys.stdout.flush()

    def _pane_box(self, label: str, pane: _Pane, name: str, width: int, height: int) -> list[str]:
        inner_w = max(1, width - 2)
        inner_h = max(1, height - 2)
        body_h = max(1, inner_h - 1)
        wrapped: list[str] = []
        for record in pane.records:
            block = _wrap_text(record, inner_w)
            if wrapped and block:
                wrapped.append("")
            wrapped.extend(block)
        pane.total = len(wrapped)
        pane.page = body_h
        latest = max(0, len(wrapped) - body_h)
        if pane.follow:
            start = latest
        else:
            start = min(pane.anchor, latest)
            pane.follow = start >= latest
        pane.anchor = start
        view = wrapped[start : start + body_h]
        focused = self._focus == name
        mark = "● " if focused else ""
        if pane.follow:
            title = f"{mark}{label}"
            hint = "↑↓/滚轮看历史 · Tab切换框 · 长日志会换行"
        else:
            title = f"{mark}{label} · 历史"
            hint = "↑↓翻阅 · PgUp/PgDn翻页 · End回到最新"
        view.append(hint)
        return _box(title, view, width, height)


board = Dashboard()
atexit.register(board.stop)

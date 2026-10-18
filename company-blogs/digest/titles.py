"""扫描已保存的公司博客，把英文标题译成中文并改文件名。不改正文。"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from digest.config import Settings
from digest.content import retitle_markdown
from digest.state import State
from digest.translate import Translator, _LETTERS, _is_chinese, configure_llm_concurrency

_BATCH = 24
_TITLE = re.compile(r'(?m)^title:\s*(.*)$')
_SOURCE = re.compile(r'(?m)^source:\s*(.*)$')


def run_titles(settings: Settings) -> int:
    notes = _english_notes(settings.output_dir)
    if not notes:
        print("[titles] 没有需要翻译的英文标题")
        return 0
    print(f"[titles] 待译英文标题 {len(notes)} 篇。只改标题和文件名，不改正文")
    configure_llm_concurrency(settings.llm_concurrency)
    translator = Translator.from_env()
    state = State(settings.state_db)
    renamed = done = 0
    rename_lock = threading.Lock()

    def one(batch: list[tuple[Path, str, str]]) -> tuple[int, int]:
        translated = translator.translate_title_batch([title for _, title, _ in batch])
        ok = 0
        for (path, title, url), title_zh in zip(batch, translated):
            if not title_zh or title_zh == title or not _is_chinese(title_zh, 0.2):
                continue
            try:
                with rename_lock:
                    target = retitle_markdown(path, title_zh, url or str(path))
                    state.rename_saved(url, title_zh, str(target))
                ok += 1
            except Exception as exc:
                print(f"[titles] 改名失败 {path.name}: {exc}", flush=True)
        return ok, len(batch)

    batches = [notes[index : index + _BATCH] for index in range(0, len(notes), _BATCH)]
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(translator.slots, len(batches)))) as pool:
            for future in as_completed(pool.submit(one, batch) for batch in batches):
                ok, count = future.result()
                renamed += ok
                done += count
                print(f"[titles] 进度 {done}/{len(notes)}，已改成中文 {renamed}", flush=True)
    finally:
        state.close()
    print(f"[titles] 完成：英文标题 {len(notes)}，改成中文 {renamed}，保留原文 {len(notes) - renamed}")
    return 0 if renamed else 1


def _english_notes(root: Path) -> list[tuple[Path, str, str]]:
    if not root.is_dir():
        return []
    notes: list[tuple[Path, str, str]] = []
    for path in sorted(root.rglob("*.md")):
        if path.parent == root or not path.is_file():
            continue
        title, url = _note_fields(path)
        if title and not _is_chinese(title, 0.35) and len(_LETTERS.findall(title)) >= 2:
            notes.append((path, title, url))
    return notes


def _note_fields(path: Path) -> tuple[str, str]:
    head = _frontmatter(path)
    return _unquote(_TITLE.search(head)), _unquote(_SOURCE.search(head))


def _frontmatter(path: Path) -> str:
    """只读到 frontmatter 结束，不把正文读进内存。"""
    try:
        with path.open(encoding="utf-8") as handle:
            if not handle.readline().startswith("---"):
                return ""
            lines: list[str] = []
            for line in handle:
                if line.startswith("---"):
                    break
                lines.append(line)
    except OSError:
        return ""
    return "".join(lines)


def _unquote(match: re.Match[str] | None) -> str:
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return re.sub(r"\s+", " ", value).strip()

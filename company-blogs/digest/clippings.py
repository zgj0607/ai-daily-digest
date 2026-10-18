"""扫描日报剪藏，把英文标题和尚未配对的英文段落一次性译成中文。"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from digest.config import Settings
from digest.content import sanitize_filename
from digest.translate import (
    Translator,
    _Block,
    _is_chinese,
    _parse_blocks,
    _should_translate,
    _strip_for_lang,
    configure_llm_concurrency,
)

DEFAULT_DIR = Path("/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings")
_H1 = re.compile(r"^#\s+(.+)$")
_FM_TITLE = re.compile(r'(?m)^title:\s*"?(.*?)"?\s*$')
_Note = tuple[Path, str, str, list[int], list[_Block]]


def run_clippings(settings: Settings, root: Path | None = None) -> int:
    notes = _pending(root or DEFAULT_DIR)
    if not notes:
        print("[clippings] 没有需要翻译的英文标题或正文")
        return 0
    work, renames = [], []
    for note in notes:
        (work if note[3] or not _is_chinese(note[1], 0.35) else renames).append(note)
    print(f"[clippings] 待处理 {len(notes)} 篇：翻译 {len(work)}，只改文件名 {len(renames)}")
    lock = threading.Lock()
    for note in renames:
        _save(note, note[1], [], lock)
    if not work:
        print(f"[clippings] 完成 {len(renames)} 篇")
        return 0

    configure_llm_concurrency(settings.llm_concurrency)
    translator = Translator.from_env()

    def one(note: _Note) -> int:
        path, title, _, indexes, blocks = note
        try:
            title_zh, translated = title, []
            if indexes:
                carry = "" if _is_chinese(title, 0.35) else title
                got, translated = translator.translate_parts(
                    [(index, blocks[index].content) for index in indexes], carry
                )
                if got and _is_chinese(got, 0.2):
                    title_zh = got
            if not _is_chinese(title_zh, 0.35):
                title_zh = translator.translate_title(title)
            _save(note, title_zh, translated, lock)
        except Exception as exc:
            print(f"[clippings] 失败 {path.name}: {exc}", flush=True)
            return 0
        return 1

    done = 0
    workers = max(1, min(translator.slots, len(work)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed(pool.submit(one, note) for note in work):
            done += future.result()
            print(f"[clippings] 进度 {done}/{len(work)}", flush=True)
    print(f"[clippings] 完成 {done + len(renames)} 篇")
    return 0 if done or renames else 1


def _pending(root: Path) -> list[_Note]:
    if not root.is_dir():
        return []
    notes: list[_Note] = []
    for path in sorted(root.glob("*.md")):
        if not path.is_file() or "RSS简报" in path.name:
            continue
        title, head, body = _split(path.read_text(encoding="utf-8"))
        blocks = _parse_blocks(body)
        indexes = _unpaired(blocks)
        file_title = re.sub(r"^\d{8}-", "", path.stem)
        shown = title or file_title
        english = not _is_chinese(shown, 0.35) or not _is_chinese(file_title, 0.35)
        if indexes or english:
            notes.append((path, shown, head, indexes, blocks))
    return notes


def _unpaired(blocks: list[_Block]) -> list[int]:
    indexes = []
    for index, block in enumerate(blocks):
        nxt = blocks[index + 1] if index + 1 < len(blocks) else None
        translated = nxt and nxt.type == "text" and _is_chinese(nxt.content, 0.2)
        if (
            block.type == "text"
            and not translated
            and _should_translate(block.content)
            and len(_strip_for_lang(block.content)) >= 8
        ):
            indexes.append(index)
    return indexes


def _split(text: str) -> tuple[str, str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            head = text[: end + 5]
            match = _FM_TITLE.search(head)
            return (match.group(1).strip() if match else ""), head, text[end + 5 :]
    match = _H1.match(text.split("\n", 1)[0].strip())
    title = match.group(1).strip() if match else ""
    sep = text.find("\n---\n")
    if sep == -1:
        rest = text.split("\n", 1)[1] if title else text
        return title, f"# {title}\n\n" if title else "", rest
    return title, text[: sep + 5], text[sep + 5 :]


def _save(note: _Note, title_zh: str, translated: list[str], lock: threading.Lock) -> None:
    path, title, head, indexes, blocks = note
    paired = {
        index: text.strip()
        for index, text in zip(indexes, translated)
        if text and text.strip() and _is_chinese(text, 0.2)
    }
    if _is_chinese(title_zh, 0.2) and title_zh != title:
        head = _retitle(head, title_zh)
    if paired:
        parts: list[str] = []
        for index, block in enumerate(blocks):
            parts.append(block.content)
            if index in paired:
                parts.append(paired[index])
        body = "\n\n".join(parts).strip()
        path.write_text(f"{head}\n{body}\n" if head else body + "\n", encoding="utf-8")
    elif head != note[2]:
        text = path.read_text(encoding="utf-8")
        path.write_text(head + text[len(note[2]) :], encoding="utf-8")
    if _is_chinese(title_zh, 0.2):
        _rename(path, title_zh, lock)


def _retitle(head: str, title_zh: str) -> str:
    line = title_zh.replace("\n", " ").replace('"', "")
    if head.startswith("#"):
        rest = head.split("\n", 1)[1] if "\n" in head else ""
        return "# " + line + ("\n" + rest if rest else "")
    if head.startswith("---"):
        return re.sub(r"(?m)^title:.*$", f'title: "{line}"', head, count=1)
    return head


def _rename(path: Path, title_zh: str, lock: threading.Lock) -> None:
    stamp = re.match(r"^(\d{8})-", path.name)
    prefix = f"{stamp.group(1)}-" if stamp else ""
    stem = sanitize_filename(title_zh)
    with lock:
        target = path.with_name(f"{prefix}{stem}.md")
        for number in range(2, 20):
            if not target.exists() or target.resolve() == path.resolve():
                break
            target = path.with_name(f"{prefix}{stem}-{number}.md")
        else:
            return
        if target.resolve() != path.resolve():
            path.rename(target)

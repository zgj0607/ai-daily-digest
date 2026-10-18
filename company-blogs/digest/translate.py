from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from digest.config import PROJECT_ROOT
from digest.ui import board

LOCAL_DEFAULT_HOST = "192.168.110.38"
LOCAL_DEFAULT_MODEL = "qwen3.6-27b"
LOCAL_DEFAULT_API_KEY = "sk-litellm-local"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
# 云端一篇尽量一次请求。Coding Plan 每 5 小时只有 6000 次，按 8 段一批会把额度用在往返上。
_ONE_CALL_CHARS = 18000
_SPLIT_CHARS = 14000
# 本地 Qwen 上下文是 8096 token，含提示词和译文。整篇送入会失败并落到兜底模型。
_LOCAL_CONTEXT = 8096
_LOCAL_MARGIN = 96
_channel_limit = 3
# Coding Plan 并发由平台动态分配，没有公开固定值。起步 8，跑顺就加，顶到拒绝再降。
_CLOUD_FLOOR = 4
_CLOUD_START = 8
_CLOUD_CEILING = 20
_HTTP = threading.local()


def configure_llm_concurrency(limit: int) -> None:
    """本地模型的并发上限。云端 Coding Plan 使用单独的更高上限。"""
    global _channel_limit
    _channel_limit = max(1, limit)

_CN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LETTERS = re.compile(
    r"[a-zA-Z\u0400-\u04ff\u00C0-\u024F\u1E00-\u1EFF\u0370-\u03ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u3040-\u30ff\uac00-\ud7af]"
)


@dataclass
class _Block:
    type: str
    content: str


class Translator:
    def __init__(self, call, channels: int = 1, slots: int = 1, translate_jobs=None) -> None:
        self._call = call
        self.channels = channels
        self.slots = slots
        self._translate_jobs = translate_jobs

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Translator:
        _load_env_file(env_file or PROJECT_ROOT / ".env")
        call, channels, slots, translate_jobs = _build_client()
        return cls(call, channels, slots, translate_jobs)

    def complete(self, prompt: str) -> str:
        """通用 LLM 补全（与翻译共用同一套 provider / 并发限流）。"""
        return self._call(prompt)

    def translate_title(self, title: str) -> str:
        """标题译成中文。已是中文或翻译失败时保留原文，供 Obsidian 文件名使用。"""
        cleaned = re.sub(r"\s+", " ", title or "").strip()
        if not cleaned or _is_chinese(cleaned, 0.35):
            return cleaned
        prompt = (
            "你是技术编辑。将文章标题译成自然、简洁的中文。"
            "已经是中文则原样返回。只返回标题一行，不要引号、序号或解释。\n\n"
            + cleaned
        )
        try:
            text = self.complete(prompt)
        except Exception:
            return cleaned
        return _accept_title(cleaned, text) or cleaned

    def translate_title_batch(self, titles: list[str]) -> list[str]:
        """一批标题一次请求。对不上序号的条目再走 translate_title。"""
        cleaned = [re.sub(r"\s+", " ", title or "").strip() for title in titles]
        if len(cleaned) <= 1:
            return [self.translate_title(title) for title in cleaned]
        prompt = (
            "你是技术编辑。把每个 [[序号]] 后的文章标题译成自然、简洁的中文。\n"
            "必须原样保留每一个 [[序号]]，译文紧跟在自己的标记后面。不要合并、漏序号或解释。\n\n"
            + "\n\n".join(f"[[{index}]]\n{title}" for index, title in enumerate(cleaned))
        )
        try:
            marked = _from_markers(self.complete(prompt), cleaned)
        except Exception:
            marked = None
        if not marked:
            return [self.translate_title(title) for title in cleaned]
        return [_accept_title(original, text) or self.translate_title(original) for original, text in zip(cleaned, marked)]

    def translate_parts(self, parts: list[tuple[int, str]], title: str = "") -> tuple[str, list[str]]:
        """整篇一次请求。parts 是 (序号, 原文)，返回 (中文标题, 与 parts 对齐的译文)。"""
        if self._translate_jobs is not None:
            return self._translate_jobs(parts, title)
        return _translate_blocks([content for _, content in parts], self._call, title)

    def add_bilingual(self, markdown: str, title: str = "") -> tuple[str, str]:
        """译正文，并把需要翻译的标题放进第一批请求。返回 (中文标题, 双语正文)。"""
        original_title = re.sub(r"\s+", " ", title or "").strip()
        want_title = bool(original_title) and not _is_chinese(original_title, 0.35)
        blocks = _parse_blocks(markdown)
        all_text = "\n".join(block.content for block in blocks if block.type == "text")
        if not all_text.strip() or _is_chinese(all_text, 0.35):
            translated = self.translate_title(original_title) if want_title else original_title
            return translated, markdown

        jobs = [
            (index, block.content)
            for index, block in enumerate(blocks)
            if block.type == "text" and _should_translate(block.content)
        ]
        if not jobs:
            translated = self.translate_title(original_title) if want_title else original_title
            return translated, markdown

        carry = original_title if want_title else ""
        got_title, translated_list = self.translate_parts(jobs, carry)
        title_zh = got_title or original_title
        if want_title and title_zh == original_title:
            title_zh = self.translate_title(original_title)

        translations: dict[int, str] = {}
        failed = 0
        for (index, content), text in zip(jobs, translated_list):
            cleaned = text.strip()
            if cleaned and cleaned != content.strip():
                translations[index] = cleaned
            else:
                failed += 1
        if not translations:
            if failed:
                board.log_translate(f"[translate] 翻译失败（{failed} 段未译出），保留原文")
            return title_zh, markdown
        if failed:
            board.log_translate(f"[translate] 警告: {failed} 个段落未译出，已译段落保留双语，其余保留原文")

        parts: list[str] = []
        for index, block in enumerate(blocks):
            parts.append(block.content)
            translated = translations.get(index)
            if translated:
                parts.append(translated)
        return title_zh, "\n\n".join(parts).strip()


def _build_client():
    local_base = (
        os.environ.get("LOCAL_LLM_API_BASE", "").strip()
        or f"http://{os.environ.get('AI_UBUNTU_HOST', '').strip() or LOCAL_DEFAULT_HOST}:8000/v1"
    ).rstrip("/")
    local_model = os.environ.get("LOCAL_LLM_MODEL", "").strip() or LOCAL_DEFAULT_MODEL
    local_key = (
        os.environ.get("LOCAL_LLM_API_KEY", "").strip()
        or os.environ.get("LITELLM_MASTER_KEY", "").strip()
        or LOCAL_DEFAULT_API_KEY
    )
    cloud_key = os.environ.get("OPENAI_API_KEY", "").strip()
    cloud_base = os.environ.get("OPENAI_API_BASE", "").strip().rstrip("/")
    cloud_model = os.environ.get("OPENAI_MODEL", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    # 本地按 llm_concurrency，并且先按 8096 token 切段。云端整篇一次请求。
    def _call_local(prompt: str) -> str:
        room = _output_room(prompt)
        if room is None:
            raise RuntimeError(f"本地上下文不足（窗口 {_LOCAL_CONTEXT} token）")
        return _call_openai(prompt, local_key, local_base, local_model, max_tokens=room)

    pool: list[tuple[str, object, int, str]] = [
        (f"本地 Qwen ({local_base}, model={local_model})", _call_local, _channel_limit, "local"),
    ]
    if cloud_key and cloud_base and cloud_base != local_base:
        pool.append((
            f"云端 Qwen ({cloud_base}, model={cloud_model or 'qwen'})",
            lambda prompt, key=cloud_key, base=cloud_base, model=cloud_model: _call_openai(
                prompt,
                key,
                base,
                model or "qwen-plus",
                max_tokens=16384,
                extra={"enable_thinking": False},
            ),
            _CLOUD_CEILING,
            "cloud",
        ))
    fallbacks: list[tuple[str, object]] = []
    if gemini_key:
        fallbacks.append(("Gemini", lambda prompt: _call_gemini(prompt, gemini_key)))

    inflight = [0] * len(pool)
    closed: set[int] = set()
    cond = threading.Condition()
    live = [_CLOUD_START if kind == "cloud" else limit for _name, _provider, limit, kind in pool]
    pressure_until = 0.0
    cloud_done = 0
    print(
        "[translate] "
        + " | ".join(
            f"{name} 同时 {live[i]} 篇"
            + (f"，最多 {limit} 篇" if kind == "cloud" else "")
            + ("，按 8096 token 切段" if kind == "local" else "，整篇一次请求")
            for i, (name, _, limit, kind) in enumerate(pool)
        )
        + "。云端跑顺会加并发；超限只降速重试，5 小时额度用尽才关闭"
    )

    def _close_one(index: int) -> bool:
        with cond:
            first = index not in closed
            closed.add(index)
            cond.notify_all()
            return first

    def _reserve(exclude: set[int]) -> int | None:
        with cond:
            while True:
                opened = [index for index in range(len(pool)) if index not in exclude and index not in closed]
                if not opened:
                    return None
                opened.sort(key=lambda index: inflight[index])
                for index in opened:
                    if inflight[index] < live[index]:
                        inflight[index] += 1
                        return index
                cond.wait()

    def _release(index: int) -> None:
        with cond:
            inflight[index] -= 1
            cond.notify_all()

    def _invoke(index: int, prompt: str) -> str:
        name, provider, _, _kind = pool[index]
        text = provider(prompt)
        if not str(text).strip():
            raise RuntimeError(f"{name} 返回内容为空")
        return str(text)

    def _pressure(index: int, *, usage: bool) -> None:
        nonlocal pressure_until
        with cond:
            pressure_until = time.time() + 45
            if usage:
                live[index] = max(_CLOUD_FLOOR, live[index] - 2)
            else:
                live[index] = max(_CLOUD_FLOOR, min(live[index], inflight[index] - 1))
            cond.notify_all()

    def _note_success(index: int) -> None:
        nonlocal cloud_done
        if pool[index][3] != "cloud":
            return
        with cond:
            if time.time() < pressure_until or live[index] >= pool[index][2]:
                return
            cloud_done += 1
            if cloud_done % 6 != 0:
                return
            live[index] += 1
            cond.notify_all()
            current = live[index]
        board.log_translate(f"[translate] 云端并发提到 {current}")

    def _cloud_pause(index: int, exc: BaseException, waits: int) -> float | None:
        """云端并发或短时用量超限时返回等待秒数。其他错误返回 None。"""
        if pool[index][3] != "cloud":
            return None
        name = pool[index][0]
        if _is_concurrency_wait(exc) and waits < 12:
            _pressure(index, usage=False)
            board.log_translate(f"[translate] {name} 并发超限，降到 {live[index]} 后重试。原因: {exc}")
            return min(3.0, 0.25 * (waits + 1))
        if _is_usage_wait(exc) and waits < 5:
            _pressure(index, usage=True)
            board.log_translate(f"[translate] {name} 短时用量超限，降到 {live[index]}，60 秒后重试。原因: {exc}")
            return 60.0
        return None

    def _send_held(index: int, prompt: str) -> str:
        """占着当前名额重试。并发超限只降低后续准入，不把这篇改交给兜底模型。"""
        waits = 0
        while True:
            try:
                text = _invoke(index, prompt)
            except Exception as exc:
                pause = _cloud_pause(index, exc, waits)
                if pause is None:
                    raise
                waits += 1
                time.sleep(pause)
                continue
            _note_success(index)
            return text

    def call(prompt: str, skip: set[int] | None = None) -> str:
        errors: list[str] = []
        tried = set(skip or ())
        for index, (_name, _provider, _limit, kind) in enumerate(pool):
            if kind == "local" and _output_room(prompt) is None:
                tried.add(index)
        waits = 0
        while True:
            index = _reserve(tried)
            if index is None:
                break
            tried.add(index)
            name = pool[index][0]
            pause = 0.0
            try:
                return _invoke(index, prompt)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                if _is_plan_exhausted(exc):
                    if _close_one(index):
                        board.log_translate(f"[translate] {name} 套餐额度用尽，已关闭该模型。原因: {exc}")
                elif (cloud_pause := _cloud_pause(index, exc, waits)) is not None:
                    waits += 1
                    tried.discard(index)
                    pause = cloud_pause
                elif _is_rate_limited(exc) and waits < 4:
                    waits += 1
                    tried.discard(index)
                    pause = 15.0
                    board.log_translate(f"[translate] {name} 触发限流，15 秒后重试，不关闭模型。原因: {exc}")
                else:
                    board.log_translate(f"[translate] {name} 失败，改试其他模型。原因: {exc}")
            finally:
                _release(index)
            if pause:
                time.sleep(pause)
        for name, provider in fallbacks:
            try:
                text = provider(prompt)
                if str(text).strip():
                    return str(text)
                raise RuntimeError("返回内容为空")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("所有翻译接口都失败: " + " | ".join(errors))

    def translate_jobs(jobs: list[tuple[int, str]], title: str) -> tuple[str, list[str]]:
        """先占一个模型名额。本地按段落切到 8096 token 内；云端仍整篇一次请求。"""
        index = _reserve(set())
        if index is None:
            return _translate_blocks([content for _, content in jobs], call, title)
        local = pool[index][3] == "local"
        try:
            batches = _pack_local_jobs(jobs, title) if local else _pack_jobs(jobs, _ONE_CALL_CHARS, _SPLIT_CHARS)
            return _collect_translations(jobs, batches, title, lambda prompt, index=index: _send_held(index, prompt), local)
        except Exception as exc:
            failed = index
            board.log_translate(f"[translate] {pool[failed][0]} 失败，改试其他模型。原因: {exc}")
            _release(index)
            index = None
            skip = {i for i, item in enumerate(pool) if item[3] == "local"} if local else {failed}
            return _collect_translations(
                jobs,
                _pack_jobs(jobs, _ONE_CALL_CHARS, _SPLIT_CHARS),
                title,
                lambda prompt: call(prompt, skip),
                False,
            )
        finally:
            if index is not None:
                _release(index)

    return call, len(pool), sum(item[2] for item in pool), translate_jobs


def _http() -> requests.Session:
    session = getattr(_HTTP, "session", None)
    if session is None:
        session = requests.Session()
        _HTTP.session = session
    return session


def _pack_jobs(
    jobs: list[tuple[int, str]],
    one_call_chars: int,
    split_chars: int,
) -> list[list[tuple[int, str]]]:
    """短文一次请求；更长的文章按字数切开，避免单次输出被截断。"""
    total = sum(len(content) for _, content in jobs)
    if total <= one_call_chars:
        return [jobs]
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for job in jobs:
        length = len(job[1])
        if current and size + length > split_chars:
            batches.append(current)
            current = []
            size = 0
        current.append(job)
        size += length
    if current:
        batches.append(current)
    return batches


def _estimate_tokens(text: str) -> int:
    """偏保守的 Qwen token 估算：中文约 1.2 字一个 token，其余约 2.5 字符一个。"""
    if not text:
        return 0
    cjk = len(_CN.findall(text))
    other = len(text) - cjk
    return int(cjk / 1.2 + other / 2.5) + 1


def _output_room(prompt: str) -> int | None:
    """本地输出还剩多少 token。提示词本身放不下时返回 None，避免整篇请求打到本地。"""
    room = _LOCAL_CONTEXT - _estimate_tokens(prompt) - _LOCAL_MARGIN
    if room < 256:
        return None
    return room


def _translation_prompt(blocks: list[str], title: str = "") -> str:
    marked = "\n\n".join(f"[[{index}]]\n{block}" for index, block in enumerate(blocks))
    title_rule = "第一行只写“标题：”和译好的一行中文标题，然后空一行。标题已是中文则原样放在这一行。\n" if title else ""
    return (
        "你是沉浸式翻译助手。下面每个 [[序号]] 对应一个原文段落，请逐段译成中文。\n"
        "必须原样保留每一个 [[序号]]，该段译文紧跟在自己的标记后面。\n"
        "一段只译一段：不要合并，不要拆开，不要调换顺序，不要漏序号，不要把整篇合成一段。\n"
        "不要重复原文，不要输出解释。保留链接、图片和 Markdown 格式。已是中文的段落，标记后留空。\n"
        + title_rule
        + (f"\n待译标题：{title}\n\n" if title else "\n")
        + marked
    )


def _local_budget_ok(blocks: list[str], title: str) -> bool:
    if not blocks:
        return True
    room = _output_room(_translation_prompt(blocks, title))
    if room is None:
        return False
    output_tokens = int(sum(_estimate_tokens(block) for block in blocks) * 1.35) + 48
    return room >= output_tokens


_SENTENCE = re.compile(r"(?<=[。！？.!?])\s+")


def _slice_chars(content: str, title: str) -> list[str]:
    parts: list[str] = []
    rest = content.strip()
    guard = 0
    while rest and guard < 2000:
        guard += 1
        if _local_budget_ok([rest], title):
            parts.append(rest)
            break
        lo, hi, best = 1, len(rest), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if _local_budget_ok([rest[:mid]], title):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best < 1:
            best = min(len(rest), 200)
        cut = rest.rfind(" ", max(1, best // 2), best + 1)
        if cut < 1:
            cut = best
        piece = rest[:cut].strip()
        if not piece:
            break
        parts.append(piece)
        nxt = rest[cut:].strip()
        if nxt == rest:
            break
        rest = nxt
    return parts or [content.strip()]


def _fitting_pieces(index: int, content: str, title: str) -> list[tuple[int, str]]:
    if _local_budget_ok([content], title):
        return [(index, content)]
    parts = [part.strip() for part in _SENTENCE.split(content) if part.strip()] or [content]
    pieces: list[tuple[int, str]] = []
    for part in parts:
        if _local_budget_ok([part], title):
            pieces.append((index, part))
        else:
            pieces.extend((index, bit) for bit in _slice_chars(part, title))
    return pieces


def _pack_local_jobs(jobs: list[tuple[int, str]], title: str) -> list[list[tuple[int, str]]]:
    """本地默认按段落切开，每批提示词加译文都落在 8096 token 以内。"""
    pieces: list[tuple[int, str]] = []
    for index, content in jobs:
        pieces.extend(_fitting_pieces(index, content, title))
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for piece in pieces:
        trial = current + [piece]
        if current and not _local_budget_ok([content for _, content in trial], title if not batches else ""):
            batches.append(current)
            current = [piece]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def _collect_translations(jobs, batches, title: str, send, local: bool) -> tuple[str, list[str]]:
    collected: dict[int, list[str]] = {}
    best_title = ""
    for batch_index, batch in enumerate(batches):
        got, texts = _translate_fitting(batch, title if batch_index == 0 else "", send, local)
        if got and not best_title:
            best_title = got
        for (block_index, _), text in zip(batch, texts):
            cleaned = text.strip()
            if cleaned:
                collected.setdefault(block_index, []).append(cleaned)
    return best_title, ["\n".join(collected.get(block_index, [])) for block_index, _ in jobs]


def _hard_halves(content: str) -> list[str]:
    text = content.strip()
    if len(text) < 80:
        return [text]
    sentences = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if len(sentences) >= 2:
        mid = len(sentences) // 2
        left = " ".join(sentences[:mid]).strip()
        right = " ".join(sentences[mid:]).strip()
        if left and right and left != text and right != text:
            return [left, right]
    cut = max(1, len(text) // 2)
    space = text.rfind(" ", 0, cut)
    if space > len(text) // 5:
        cut = space
    left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        return [text]
    return [left, right]


def _is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "context length",
            "context_length",
            "maximum context",
            "max context",
            "context window",
            "too many tokens",
            "token limit",
            "input is too long",
            "reduce the length",
            "上下文不足",
            "上下文长度",
        )
    )


def _translate_fitting(
    pieces: list[tuple[int, str]],
    title: str,
    send,
    local: bool,
) -> tuple[str, list[str]]:
    """返回与 pieces 等长的译文。本地超出窗口时对半再切，不把这篇交给兜底模型。"""
    try:
        return _translate_blocks([content for _, content in pieces], send, title, strict=True)
    except Exception as exc:
        if not local or not _is_context_overflow(exc) or not pieces:
            raise
        if len(pieces) > 1:
            mid = len(pieces) // 2
            first_title, first = _translate_fitting(pieces[:mid], title, send, True)
            second_title, second = _translate_fitting(pieces[mid:], "", send, True)
            return first_title or second_title, first + second
        block_index, content = pieces[0]
        halves = _hard_halves(content)
        if len(halves) < 2:
            raise
        got = ""
        chunks: list[str] = []
        for part_index, part in enumerate(halves):
            part_title, one = _translate_fitting([(block_index, part)], title if part_index == 0 else "", send, True)
            got = got or part_title
            if one and one[0].strip():
                chunks.append(one[0].strip())
        return got, ["\n".join(chunks)]


def _is_concurrency_wait(exc: BaseException) -> bool:
    return "concurrency" in str(exc).lower()


def _is_usage_wait(exc: BaseException) -> bool:
    return "usage allocated quota" in str(exc).lower()


def _is_plan_exhausted(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("hour allocated quota", "week allocated quota", "month allocated quota"))


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in text or ("403" in text and "quota" in text):
        return True
    return any(
        token in text
        for token in (
            "rate limit",
            "ratelimit",
            "too many requests",
            "throttl",
            "quota",
            "freetier",
            "限流",
            "请求频率",
            "requestlimit",
            "免费额度",
            "额度不足",
            "额度已用",
        )
    )


def _accept_title(original: str, translated: str) -> str:
    text = (translated or "").strip().splitlines()[0].strip() if translated else ""
    text = text.strip("\"'「」『』").strip()
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) > 180 or text == original:
        return ""
    return text


def _call_gemini(prompt: str, api_key: str) -> str:
    response = _http().post(
        GEMINI_API_URL,
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "topP": 0.8, "topK": 40},
        },
        timeout=300,
    )
    if not response.ok:
        raise RuntimeError(f"Gemini API error ({response.status_code}): {response.text[:500]}")
    data = response.json()
    candidates = data.get("candidates") or []
    parts = (((candidates[0].get("content") or {}).get("parts")) if candidates else None) or []
    text = parts[0].get("text") if parts else ""
    return text or ""


def _call_openai(
    prompt: str,
    api_key: str,
    api_base: str,
    model: str,
    *,
    qwen_thinking: bool = True,
    temperature: float | None = 0.3,
    top_p: float | None = 0.8,
    max_tokens: int | None = None,
    extra: dict | None = None,
) -> str:
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if qwen_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if extra:
        body.update(extra)
    response = _http().post(
        f"{api_base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=(10, 600),
    )
    if not response.ok:
        raise RuntimeError(f"OpenAI-compatible API error ({response.status_code}): {response.text[:500]}")
    message = ((response.json().get("choices") or [{}])[0].get("message") or {})
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        text = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )
        if text:
            return text
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _translate_blocks(blocks: list[str], call, title: str = "", strict: bool = False) -> tuple[str, list[str]]:
    if not blocks and not title:
        return "", []
    prompt = _translation_prompt(blocks, title)
    best_title = ""
    for _ in range(2):
        try:
            raw = call(prompt)
        except Exception:
            if strict:
                raise
            continue
        got_title, output = _align_translations(raw, blocks, title)
        if got_title:
            best_title = got_title
        if any(output) or not blocks:
            return best_title, output
    return best_title, [""] * len(blocks)


_MARKER = re.compile(r"\[\[(\d+)\]\]")


def _align_translations(raw: str, blocks: list[str], title: str) -> tuple[str, list[str]]:
    """按序号把译文对回原段落。对不上的段落留空，避免整篇译文堆到某一段下面。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|markdown)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    got_title, text = _peel_title(text, title)
    marked = _from_markers(text, blocks)
    if marked is not None:
        return got_title, marked
    parsed_title, parsed = _from_json(text, blocks, title)
    return got_title or parsed_title, parsed


def _peel_title(text: str, title: str) -> tuple[str, str]:
    lines = text.splitlines()
    for index, line in enumerate(lines[:4]):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("标题"):
            payload = re.split(r"[:：]", stripped, maxsplit=1)
            got = _accept_title(title, payload[1] if len(payload) > 1 else "")
            rest = "\n".join(lines[:index] + lines[index + 1 :])
            return got, rest.strip()
        break
    return "", text


def _from_markers(text: str, blocks: list[str]) -> list[str] | None:
    matches = list(_MARKER.finditer(text))
    if not matches:
        return None
    chunks: list[tuple[int, str]] = []
    seen: set[int] = set()
    for index, match in enumerate(matches):
        slot = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if slot in seen:
            continue
        seen.add(slot)
        chunks.append((slot, text[match.end() : end]))
    output = _place_pairs(blocks, chunks)
    return output if any(output) else None


def _from_json(text: str, blocks: list[str], title: str) -> tuple[str, list[str]]:
    empty = [""] * len(blocks)
    try:
        data = parse_llm_json(text)
    except Exception:
        return "", empty
    got_title = _accept_title(title, str(data.get("title") or "")) if title else ""
    items = data.get("translations")
    if not isinstance(items, list) or not items:
        return got_title, empty
    indexed: list[tuple[object, str]] = []
    positional: list[str] = []
    has_index = False
    for item in items:
        if isinstance(item, str):
            positional.append(item)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("text")
        if not isinstance(value, str):
            continue
        if item.get("index") is not None:
            has_index = True
            indexed.append((item.get("index"), value))
        else:
            positional.append(value)
    if has_index:
        pairs = [(slot, value) for raw, value in indexed if (slot := _coerce_index(raw)) is not None]
        return got_title, _place_pairs(blocks, pairs)
    # 没有序号时，只有条数与段落数完全一致才按顺序填入，避免少一条就整篇错位
    if len(positional) == len(blocks):
        return got_title, _place_pairs(blocks, list(enumerate(positional)))
    return got_title, empty


def _place_pairs(blocks: list[str], pairs: list[tuple[int, str]]) -> list[str]:
    output = [""] * len(blocks)
    for slot, text in _shift_one_based(pairs, len(blocks)):
        if 0 <= slot < len(blocks) and not output[slot]:
            output[slot] = _accept_chunk(text, blocks, slot)
    return output


def _shift_one_based(pairs: list[tuple[int, str]], count: int) -> list[tuple[int, str]]:
    """模型有时从 1 开始编号。只有序号刚好是 1..N 时才整体减一，缺号时保持原序号。"""
    if count > 0 and {slot for slot, _ in pairs} == set(range(1, count + 1)):
        return [(slot - 1, value) for slot, value in pairs]
    return pairs


def _coerce_index(value: object) -> int | None:
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _accept_chunk(chunk: str, blocks: list[str], index: int) -> str:
    text = chunk.strip()
    source = blocks[index].strip()
    if text.startswith(source):
        text = text[len(source) :].strip()
    if not text or text == source:
        return ""
    if _swallowed_other(text, blocks, index):
        return ""
    if len(blocks) >= 2 and len(text) > 600 and len(text) > max(len(source) * 3, len(source) + 800):
        return ""
    return text


def _swallowed_other(text: str, blocks: list[str], index: int) -> bool:
    folded = " ".join(text.split())
    for other_index, other in enumerate(blocks):
        if other_index == index:
            continue
        probe = " ".join(other.split())[:48]
        if len(probe) >= 48 and probe in folded:
            return True
    return False


def parse_llm_json(text: str) -> dict:
    """解析模型返回的 JSON（兼容 ```json 围栏与前后杂质）。"""
    json_text = text.strip()
    if json_text.startswith("```"):
        json_text = re.sub(r"^```(?:json)?\n?", "", json_text)
        json_text = re.sub(r"\n?```$", "", json_text)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", json_text, flags=re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise RuntimeError("LLM 返回的 JSON 不是对象")
    return data


def _parse_blocks(markdown: str) -> list[_Block]:
    blocks: list[_Block] = []
    lines = markdown.split("\n")
    buf: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal buf
        content = "\n".join(buf).rstrip()
        buf = []
        if content.strip():
            blocks.append(_Block("text", content))

    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            flush()
            code = [line]
            index += 1
            while index < len(lines):
                code.append(lines[index])
                if lines[index].strip().startswith("```") and len(code) > 1:
                    index += 1
                    break
                index += 1
            blocks.append(_Block("code", "\n".join(code)))
            continue
        if not line.strip():
            flush()
            index += 1
            continue
        buf.append(line)
        index += 1
    flush()
    return blocks


def _strip_for_lang(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"[#>*_\[\]()!~`|]", "", text)
    return re.sub(r"\s+", "", text).strip()


def _is_chinese(text: str, ratio: float = 0.2) -> bool:
    plain = _strip_for_lang(text)
    if not plain:
        return False
    chinese = len(_CN.findall(plain))
    if not chinese:
        return False
    letters = chinese + len(_LETTERS.findall(plain))
    return letters > 0 and chinese / letters >= ratio


def _should_translate(content: str) -> bool:
    plain = re.sub(r"^#{1,6}\s+", "", content).strip()
    image_only = bool(re.fullmatch(r"!\[.*\]\(.*\)", plain))
    return (
        len(plain) >= 8
        and not _is_chinese(content)
        and not image_only
        and not plain.startswith(("http://", "https://"))
        and len(_LETTERS.findall(plain)) >= 4
    )


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

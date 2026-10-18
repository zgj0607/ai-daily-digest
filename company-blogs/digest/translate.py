from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from digest.config import PROJECT_ROOT
from digest.ui import board

LOCAL_DEFAULT_HOST = "192.168.110.38"
LOCAL_DEFAULT_MODEL = "qwen3.6-27b"
LOCAL_DEFAULT_API_KEY = "sk-litellm-local"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
# 一篇尽量一次请求。Coding Plan 每 5 小时只有 6000 次，按 8 段一批会把额度用在往返上。
_ONE_CALL_CHARS = 18000
_SPLIT_CHARS = 14000
_channel_limit = 3
_CLOUD_LIMIT = 8
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
    def __init__(self, call, channels: int = 1, slots: int = 1) -> None:
        self._call = call
        self.channels = channels
        self.slots = slots

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Translator:
        _load_env_file(env_file or PROJECT_ROOT / ".env")
        call, channels, slots = _build_client()
        return cls(call, channels, slots)

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

        batches = _pack_jobs(jobs, _ONE_CALL_CHARS, _SPLIT_CHARS)

        def worker(item: tuple[int, list[tuple[int, str]]]) -> tuple[dict[int, str], int, str]:
            batch_index, batch = item
            carry = original_title if want_title and batch_index == 0 else ""
            got_title, translated = _translate_blocks([content for _, content in batch], self._call, carry)
            found: dict[int, str] = {}
            missing = 0
            for (index, content), text in zip(batch, translated):
                cleaned = text.strip()
                if cleaned and cleaned != content.strip():
                    found[index] = cleaned
                else:
                    missing += 1
            return found, missing, got_title

        # 各批并行发给当时有空的模型；模型名额仍由 call() 按每个模型限制
        width = min(len(batches), max(1, self.slots))
        enumerated = list(enumerate(batches))
        if width == 1:
            results = [worker(item) for item in enumerated]
        else:
            with ThreadPoolExecutor(max_workers=width) as pool:
                results = list(pool.map(worker, enumerated))
        title_zh = original_title

        translations: dict[int, str] = {}
        failed = 0
        for found, missing, got_title in results:
            translations.update(found)
            failed += missing
            if got_title:
                title_zh = got_title

        if not translations:
            if failed:
                board.log_translate(f"[translate] 翻译失败（{failed} 段未译出），保留原文")
            if want_title and title_zh == original_title:
                title_zh = self.translate_title(original_title)
            return title_zh, markdown
        if failed:
            board.log_translate(f"[translate] 警告: {failed} 个段落未译出，已译段落保留双语，其余保留原文")
        if want_title and title_zh == original_title:
            title_zh = self.translate_title(original_title)

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

    # 本地按 llm_concurrency。云端单独提高并发：每篇一次长请求，靠并行把墙钟压进几小时。
    pool: list[tuple[str, object, int]] = [
        (f"本地 Qwen ({local_base}, model={local_model})",
         lambda prompt: _call_openai(prompt, local_key, local_base, local_model, max_tokens=8192),
         _channel_limit),
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
            _CLOUD_LIMIT,
        ))
    fallbacks: list[tuple[str, object]] = []
    if gemini_key:
        fallbacks.append(("Gemini", lambda prompt: _call_gemini(prompt, gemini_key)))

    inflight = [0] * len(pool)
    closed: set[int] = set()
    cond = threading.Condition()
    limits = [item[2] for item in pool]
    print(
        "[translate] "
        + " | ".join(f"{name} 同时 {limit} 篇" for name, _, limit in pool)
        + "。并发超限会等待重试；5 小时额度用尽才关闭该模型"
    )

    def _opened(exclude: set[int]) -> list[int]:
        return [index for index in range(len(pool)) if index not in exclude and index not in closed]

    def _close_one(index: int) -> bool:
        with cond:
            first = index not in closed
            closed.add(index)
            cond.notify_all()
            return first

    def _reserve(exclude: set[int]) -> int | None:
        with cond:
            while True:
                opened = _opened(exclude)
                if not opened:
                    return None
                opened.sort(key=lambda index: inflight[index])
                for index in opened:
                    if inflight[index] < limits[index]:
                        inflight[index] += 1
                        return index
                cond.wait()

    def _release(index: int) -> None:
        with cond:
            inflight[index] -= 1
            cond.notify_all()

    def _invoke(index: int, prompt: str) -> str:
        name, provider, _ = pool[index]
        text = provider(prompt)
        if not str(text).strip():
            raise RuntimeError(f"{name} 返回内容为空")
        return str(text)

    def call(prompt: str) -> str:
        errors: list[str] = []
        tried: set[int] = set()
        waits = 0
        while True:
            index = _reserve(tried)
            if index is None:
                break
            tried.add(index)
            name, _, _ = pool[index]
            pause = 0.0
            try:
                return _invoke(index, prompt)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                if _is_plan_exhausted(exc):
                    if _close_one(index):
                        board.log_translate(f"[translate] {name} 套餐额度用尽，已关闭该模型。原因: {exc}")
                elif _is_concurrency_wait(exc) and waits < 8:
                    waits += 1
                    tried.discard(index)
                    pause = min(5.0, 0.4 * waits)
                    board.log_translate(f"[translate] {name} 并发超限，等待后重试。原因: {exc}")
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

    return call, len(pool), sum(limits)


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


def _is_concurrency_wait(exc: BaseException) -> bool:
    return "concurrency" in str(exc).lower()


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


def _translate_blocks(blocks: list[str], call, title: str = "") -> tuple[str, list[str]]:
    if not blocks and not title:
        return "", []
    marked = "\n\n".join(f"[[{index}]]\n{block}" for index, block in enumerate(blocks))
    title_rule = "第一行只写“标题：”和译好的一行中文标题，然后空一行。标题已是中文则原样放在这一行。\n" if title else ""
    prompt = (
        "你是沉浸式翻译助手。下面每个 [[序号]] 对应一个原文段落，请逐段译成中文。\n"
        "必须原样保留每一个 [[序号]]，该段译文紧跟在自己的标记后面。\n"
        "一段只译一段：不要合并，不要拆开，不要调换顺序，不要漏序号，不要把整篇合成一段。\n"
        "不要重复原文，不要输出解释。保留链接、图片和 Markdown 格式。已是中文的段落，标记后留空。\n"
        + title_rule
        + (f"\n待译标题：{title}\n\n" if title else "\n")
        + marked
    )
    best_title = ""
    for _ in range(2):
        try:
            raw = call(prompt)
        except Exception:
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
    chunks = _shift_one_based(chunks, len(blocks))
    output = [""] * len(blocks)
    for slot, chunk in chunks:
        if 0 <= slot < len(blocks) and not output[slot]:
            output[slot] = _accept_chunk(chunk, blocks, slot)
    if not any(output):
        return None
    return output


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
    output = [""] * len(blocks)
    if has_index:
        pairs: list[tuple[int, str]] = []
        for raw_index, value in indexed:
            slot = _coerce_index(raw_index)
            if slot is not None:
                pairs.append((slot, value))
        pairs = _shift_one_based(pairs, len(blocks))
        for slot, value in pairs:
            if 0 <= slot < len(blocks) and not output[slot]:
                output[slot] = _accept_chunk(value, blocks, slot)
        return got_title, output
    # 没有序号时，只有条数与段落数完全一致才按顺序填入，避免少一条就整篇错位
    if len(positional) == len(blocks):
        for slot, value in enumerate(positional):
            output[slot] = _accept_chunk(value, blocks, slot)
    return got_title, output


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

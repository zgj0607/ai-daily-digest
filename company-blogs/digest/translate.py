from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from digest.config import PROJECT_ROOT
from digest.ui import board

LOCAL_DEFAULT_HOST = "192.168.110.38"
LOCAL_DEFAULT_MODEL = "qwen3.6-27b"
LOCAL_DEFAULT_API_KEY = "sk-litellm-local"
DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
KIMI_DEFAULT_BASE = "https://api.moonshot.ai/v1"
KIMI_DEFAULT_MODEL = "kimi-k2.6"
# 按量付费的免费额度在这个地址。Coding Plan 的 coding.dashscope.aliyuncs.com 不互通。
DASHSCOPE_DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_FREE_MODELS = (
    "qwen3.8-27b",
    "qwen3.7-flash-2026-07-15",
    "qwen3.8-flash",
    "kimi-k3",
    "deepseek-v4-flash-0731",
    "qwen3.8-max-0902",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "deepseek-v4-pro-0813",
    "qwen3.8-2.4t-a95b",
    "qwen3.8-max",
    "qwen3.7-flash",
    "qwen-mt-uni",
    "qwen3.8-omni-flash",
)
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
_LOCAL_BATCH = 8
_channel_limit = 3
_HTTP = threading.local()


def configure_llm_concurrency(limit: int) -> None:
    """每个模型各自的并发上限。本地与云端分开计数。"""
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
    def __init__(self, call, translate_batch: int = _LOCAL_BATCH, channels: int = 1) -> None:
        self._call = call
        self.translate_batch = translate_batch
        self.channels = channels

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Translator:
        _load_env_file(env_file or PROJECT_ROOT / ".env")
        call, batch, channels = _build_client()
        return cls(call, batch, channels)

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

        batches = [jobs[start : start + self.translate_batch] for start in range(0, len(jobs), self.translate_batch)]

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
        width = min(len(batches), max(1, self.channels * _channel_limit))
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
                parts.append("")
                parts.append(translated)
        return title_zh, re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip()


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
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    deepseek_base = (os.environ.get("DEEPSEEK_API_BASE", "").strip() or DEEPSEEK_DEFAULT_BASE).rstrip("/")
    deepseek_model = os.environ.get("DEEPSEEK_MODEL", "").strip() or DEEPSEEK_DEFAULT_MODEL
    kimi_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    kimi_base = (os.environ.get("MOONSHOT_API_BASE", "").strip() or KIMI_DEFAULT_BASE).rstrip("/")
    kimi_model = os.environ.get("MOONSHOT_MODEL", "").strip() or KIMI_DEFAULT_MODEL
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    dashscope_base = (os.environ.get("DASHSCOPE_API_BASE", "").strip() or DASHSCOPE_DEFAULT_BASE).rstrip("/")
    dashscope_models = _dashscope_models()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    # 每个模型各自最多 _channel_limit 篇同时在译。限流只关闭该模型；超时、空返回和 5xx 只改试其余模型。
    pool: list[tuple[str, object, bool]] = [
        (f"本地 Qwen ({local_base}, model={local_model})",
         lambda prompt: _call_openai(prompt, local_key, local_base, local_model),
         False),
    ]
    if cloud_key and cloud_base and cloud_base != local_base:
        pool.append((
            f"云端 Qwen ({cloud_base}, model={cloud_model or 'qwen'})",
            lambda prompt, key=cloud_key, base=cloud_base, model=cloud_model: _call_openai(
                prompt, key, base, model or "qwen-plus", extra={"enable_thinking": False},
            ),
            True,
        ))
    if deepseek_key and deepseek_base != local_base:
        pool.append((
            f"DeepSeek ({deepseek_base}, model={deepseek_model})",
            lambda prompt, key=deepseek_key, base=deepseek_base, model=deepseek_model: _call_openai(
                prompt, key, base, model, qwen_thinking=False, extra={"thinking": {"type": "disabled"}},
            ),
            True,
        ))
    if kimi_key and kimi_base != local_base:
        pool.append((
            f"Kimi ({kimi_base}, model={kimi_model})",
            lambda prompt, key=kimi_key, base=kimi_base, model=kimi_model: _call_openai(
                prompt,
                key,
                base,
                model,
                qwen_thinking=False,
                temperature=None,
                top_p=None,
                extra={"thinking": {"type": "disabled"}},
            ),
            True,
        ))
    if dashscope_key and dashscope_base != local_base:
        for model in dashscope_models:
            kwargs = _dashscope_kwargs(model)
            pool.append((
                f"百炼 {model} ({dashscope_base})",
                lambda prompt, key=dashscope_key, base=dashscope_base, model=model, kwargs=kwargs: _call_openai(
                    prompt, key, base, model, **kwargs
                ),
                True,
            ))
    fallbacks: list[tuple[str, object]] = []
    if gemini_key:
        fallbacks.append(("Gemini", lambda prompt: _call_gemini(prompt, gemini_key)))

    inflight = [0] * len(pool)
    closed: set[int] = set()
    cond = threading.Condition()
    print(
        "[translate] 每个模型同时 "
        f"{_channel_limit} 篇: "
        + " | ".join(name for name, _, _ in pool)
        + "。限流或免费额度用尽只关闭该模型，其他失败改试其余模型"
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
                    if inflight[index] < _channel_limit:
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
        while True:
            index = _reserve(tried)
            if index is None:
                break
            tried.add(index)
            name, _, is_cloud = pool[index]
            try:
                return _invoke(index, prompt)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                if is_cloud and _is_rate_limited(exc):
                    if _close_one(index):
                        board.log_translate(f"[translate] {name} 触发限流或免费额度用尽，已关闭该模型，其余模型继续。原因: {exc}")
                else:
                    board.log_translate(f"[translate] {name} 失败，改试其他模型。原因: {exc}")
            finally:
                _release(index)
        for name, provider in fallbacks:
            try:
                text = provider(prompt)
                if str(text).strip():
                    return str(text)
                raise RuntimeError("返回内容为空")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("所有翻译接口都失败: " + " | ".join(errors))

    return call, _LOCAL_BATCH, len(pool)


def _http() -> requests.Session:
    session = getattr(_HTTP, "session", None)
    if session is None:
        session = requests.Session()
        _HTTP.session = session
    return session


def _dashscope_models() -> tuple[str, ...]:
    raw = os.environ.get("DASHSCOPE_MODELS", "").strip()
    if not raw:
        return DASHSCOPE_FREE_MODELS
    models = tuple(part.strip() for part in raw.split(",") if part.strip())
    return models or DASHSCOPE_FREE_MODELS


def _dashscope_kwargs(model: str) -> dict:
    """kimi-k3 只能走思考模式，不能关思考，也不能传 temperature。"""
    if model.startswith("kimi-"):
        return {"qwen_thinking": False, "temperature": None, "top_p": None}
    extra: dict = {"enable_thinking": False}
    return {"extra": extra}


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
    if qwen_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if extra:
        body.update(extra)
    response = _http().post(
        f"{api_base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=(10, 300),
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
    title_line = f"标题：{title}\n\n" if title else ""
    schema = '{"title":"...","translations":[{"index":0,"text":"..."}]}' if title else '{"translations":[{"index":0,"text":"..."}]}'
    title_rule = "同时把标题译成自然、简洁的中文，写入 title。已经是中文则原样返回，只要一行。\n" if title else ""
    prompt = (
        "你是沉浸式翻译助手。将以下非中文 Markdown 段落逐条译为中文。已是中文的返回空字符串 \"\"。保留链接和格式。"
        + title_rule
        + "只返回 JSON：\n"
        + schema
        + "\n\n"
        + title_line
        + "\n\n".join(f"--- {index} ---\n{block}" for index, block in enumerate(blocks))
    )
    best_title = ""
    for _ in range(2):
        try:
            parsed = parse_llm_json(call(prompt))
        except Exception:
            continue
        output = [""] * len(blocks)
        for item in parsed.get("translations") or []:
            index = item.get("index")
            text = item.get("text")
            if isinstance(index, int) and 0 <= index < len(blocks) and text:
                output[index] = str(text).strip()
        if title:
            accepted = _accept_title(title, str(parsed.get("title") or ""))
            if accepted:
                best_title = accepted
        if any(output) or not blocks:
            return best_title, output
    return best_title, [""] * len(blocks)


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

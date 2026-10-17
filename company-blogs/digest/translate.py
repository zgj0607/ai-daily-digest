from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import requests

from digest.config import PROJECT_ROOT

LOCAL_DEFAULT_HOST = "192.168.110.38"
LOCAL_DEFAULT_MODEL = "qwen3.6-27b"
LOCAL_DEFAULT_API_KEY = "sk-litellm-local"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
_LOCAL_BATCH = 8
_LOCAL_CONCURRENCY = 2
_channel_limit = 3


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
    def __init__(
        self,
        call,
        translate_batch: int = _LOCAL_BATCH,
        translate_concurrency: int = _LOCAL_CONCURRENCY,
        channels: int = 1,
    ) -> None:
        self._call = call
        self.translate_batch = translate_batch
        self.translate_concurrency = translate_concurrency
        self.channels = channels

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Translator:
        _load_env_file(env_file or PROJECT_ROOT / ".env")
        call, batch, concurrency, channels = _build_client()
        return cls(call, batch, concurrency, channels)

    def complete(self, prompt: str) -> str:
        """通用 LLM 补全（与翻译共用同一套 provider / 并发限流）。"""
        return self._call(prompt)

    def add_bilingual(self, markdown: str) -> str:
        blocks = _parse_blocks(markdown)
        all_text = "\n".join(block.content for block in blocks if block.type == "text")
        if not all_text.strip() or _is_chinese(all_text, 0.35):
            return markdown

        jobs = [
            (index, block.content)
            for index, block in enumerate(blocks)
            if block.type == "text" and _should_translate(block.content)
        ]
        if not jobs:
            return markdown

        batches = [jobs[start : start + self.translate_batch] for start in range(0, len(jobs), self.translate_batch)]

        def worker(batch: list[tuple[int, str]]) -> tuple[dict[int, str], int]:
            translated = _translate_blocks([content for _, content in batch], self._call)
            found: dict[int, str] = {}
            missing = 0
            for (index, content), text in zip(batch, translated):
                cleaned = text.strip()
                if cleaned and cleaned != content.strip():
                    found[index] = cleaned
                else:
                    missing += 1
            return found, missing

        # 外层已有翻译线程 + 全局 llm 信号量，批次串行避免线程扇出
        results = [worker(batch) for batch in batches]

        translations: dict[int, str] = {}
        failed = 0
        for found, missing in results:
            translations.update(found)
            failed += missing

        if not translations:
            if failed:
                print(f"[translate] 翻译失败（{failed} 段未译出），保留原文")
            return markdown
        if failed:
            print(f"[translate] 警告: {failed} 个段落未译出，已译段落保留双语，其余保留原文")

        parts: list[str] = []
        for index, block in enumerate(blocks):
            parts.append(block.content)
            translated = translations.get(index)
            if translated:
                parts.append("")
                parts.append(translated)
        return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip()


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

    # 本地与云端千问同时接请求；谁当前更空闲就分给谁。云端限流后关闭该通道。
    pool: list[tuple[str, object, bool]] = [
        (f"本地 Qwen ({local_base}, model={local_model})",
         lambda prompt: _call_openai(prompt, local_key, local_base, local_model),
         False),
    ]
    if cloud_key and cloud_base and cloud_base != local_base:
        pool.append((
            f"云端 Qwen ({cloud_base}, model={cloud_model or 'qwen'})",
            lambda prompt, key=cloud_key, base=cloud_base, model=cloud_model: _call_openai(prompt, key, base, model or "qwen-plus"),
            True,
        ))
    fallbacks: list[tuple[str, object]] = []
    if gemini_key:
        fallbacks.append(("Gemini", lambda prompt: _call_gemini(prompt, gemini_key)))

    inflight = [0] * len(pool)
    cloud_closed = {"value": False}
    cond = threading.Condition()
    print("[translate] 翻译通道: " + " | ".join(name for name, _, _ in pool))

    def _opened(exclude: set[int]) -> list[int]:
        return [
            index
            for index in range(len(pool))
            if index not in exclude and not (pool[index][2] and cloud_closed["value"])
        ]

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

    def call(prompt: str) -> str:
        errors: list[str] = []
        tried: set[int] = set()
        while True:
            index = _reserve(tried)
            if index is None:
                break
            tried.add(index)
            name, provider, is_cloud = pool[index]
            try:
                text = provider(prompt)
                if not str(text).strip():
                    raise RuntimeError("返回内容为空")
                return str(text)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                if is_cloud and _is_rate_limited(exc):
                    with cond:
                        first = not cloud_closed["value"]
                        cloud_closed["value"] = True
                        cond.notify_all()
                    if first:
                        print(f"[translate] 云端千问触发限流（5小时6000次），后续改走本地模型。原因: {exc}")
                else:
                    print(f"[translate] {name} 失败，改试其他通道。原因: {exc}")
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

    return call, _LOCAL_BATCH, _LOCAL_CONCURRENCY, len(pool)


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in text:
        return True
    return any(
        token in text
        for token in ("rate limit", "ratelimit", "too many requests", "throttl", "quota", "限流", "请求频率", "requestlimit")
    )


def _call_gemini(prompt: str, api_key: str) -> str:
    response = requests.post(
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


def _call_openai(prompt: str, api_key: str, api_base: str, model: str) -> str:
    response = requests.post(
        f"{api_base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "top_p": 0.8,
            "chat_template_kwargs": {"enable_thinking": False},
        },
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


def _translate_blocks(blocks: list[str], call) -> list[str]:
    if not blocks:
        return []
    prompt = (
        "你是沉浸式翻译助手。将以下非中文 Markdown 段落逐条译为中文。已是中文的返回空字符串 \"\"。保留链接和格式。只返回 JSON：\n"
        '{"translations":[{"index":0,"text":"..."}]}\n\n'
        + "\n\n".join(f"--- {index} ---\n{block}" for index, block in enumerate(blocks))
    )
    try:
        parsed = parse_llm_json(call(prompt))
        output = [""] * len(blocks)
        for item in parsed.get("translations") or []:
            index = item.get("index")
            text = item.get("text")
            if isinstance(index, int) and 0 <= index < len(blocks) and text:
                output[index] = str(text).strip()
        return output
    except Exception:
        return [""] * len(blocks)


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

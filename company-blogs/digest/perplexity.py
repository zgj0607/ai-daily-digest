"""Perplexity 博客。列表页会被 Cloudflare 拦住，正文在公开的 Sanity 接口里。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from digest.content import Article
from digest.dates import parse_date
from digest.http import Fetcher

_API = "https://aqo64vfr.apicdn.sanity.io/v2025-02-19/data/query/production"
_ORIGIN = "https://www.perplexity.ai/hub/blog"
_SLUG = re.compile(rf"^{re.escape(_ORIGIN)}/([A-Za-z0-9-]+)$")
_IMAGE = re.compile(r"^image-(.+)-([a-z0-9]+)$")
_HEADING = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

_LIST = """
*[_type == "blog" && language == "en-US" && defined(slug.current) && defined(publishedAt)]
| order(publishedAt desc) { "slug": slug.current, title, publishedAt }
"""
_POST = """
*[_type == "blog" && language == "en-US" && slug.current == $slug][0]{
  title, publishedAt, summary, body, heroImage
}
"""


def is_blog(index_url: str) -> bool:
    return index_url.rstrip("/") == _ORIGIN


def posts(fetcher: Fetcher) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for item in _query(fetcher, _LIST) or []:
        slug = str(item.get("slug") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
            continue
        found.append((f"{_ORIGIN}/{slug}", str(item.get("title") or ""), str(item.get("publishedAt") or "")))
    return found


def article(fetcher: Fetcher, url: str) -> Article | None:
    match = _SLUG.fullmatch(url.rstrip("/"))
    if not match:
        return None
    doc = _query(fetcher, _POST, {"slug": match.group(1)})
    if not isinstance(doc, dict):
        raise RuntimeError(f"Sanity 没有这篇文章: {url}")
    return Article(
        title=str(doc.get("title") or ""),
        published=parse_date(str(doc.get("publishedAt") or "")),
        date_source="page",
        author="",
        description=str(doc.get("summary") or ""),
        body=_body(doc),
    )


def _query(fetcher: Fetcher, query: str, params: dict[str, str] | None = None):
    fields = {"query": " ".join(query.split())}
    for key, value in (params or {}).items():
        fields[f"${key}"] = json.dumps(value)
    response = fetcher.get(f"{_API}?{urlencode(fields)}")
    data = response.json()
    if "error" in data and "result" not in data:
        raise RuntimeError(f"Sanity 查询失败: {data['error']}")
    return data.get("result")


def _body(doc: dict) -> str:
    lines: list[str] = []
    hero = _image(doc.get("heroImage") or {})
    if hero:
        lines.append(hero)
    for block in doc.get("body") or []:
        if not isinstance(block, dict):
            continue
        rendered = _block(block)
        if rendered:
            lines.append(rendered)
    return "\n\n".join(lines).strip()


def _block(block: dict) -> str:
    kind = block.get("_type")
    if kind == "block":
        return _text_block(block)
    if kind == "image":
        return _image(block)
    if kind == "code":
        return f"```{block.get('language') or ''}\n{block.get('code') or ''}\n```"
    if kind == "latex":
        return f"$$\n{block.get('body') or ''}\n$$"
    if kind == "blogQuoteBox":
        quote = str(block.get("quote") or "").strip()
        lines = [f"> {line}" if line else ">" for line in quote.splitlines()]
        if block.get("attribution"):
            lines.append(f"> — {block['attribution']}")
        return "\n".join(lines)
    if kind == "youtube":
        video = block.get("videoId") or ""
        return f"https://www.youtube.com/watch?v={video}" if video else str(block.get("url") or "")
    if kind == "videoEmbed":
        return str(block.get("url") or "")
    if kind == "table":
        return _table(block)
    return ""


def _text_block(block: dict) -> str:
    text = _spans(block)
    if not text:
        return ""
    kind = block.get("listItem")
    if kind == "bullet":
        return f"- {text}"
    if kind == "number":
        return f"1. {text}"
    prefix = _HEADING.get(str(block.get("style") or ""))
    if prefix:
        return f"{prefix} {text}"
    if block.get("style") == "blockquote":
        return "> " + text.replace("\n", "\n> ")
    return text


def _spans(block: dict) -> str:
    marks = {item.get("_key"): item for item in block.get("markDefs") or []}
    parts: list[str] = []
    for child in block.get("children") or []:
        text = str(child.get("text") or "")
        for mark in child.get("marks") or []:
            if mark == "strong":
                text = f"**{text}**"
            elif mark == "em":
                text = f"*{text}*"
            elif mark == "code":
                text = f"`{text}`"
            elif (marks.get(mark) or {}).get("_type") == "link" and (marks[mark].get("href") or ""):
                text = f"[{text}]({marks[mark]['href']})"
        parts.append(text)
    return "".join(parts).strip()


def _image(block: dict) -> str:
    ref = str((block.get("asset") or {}).get("_ref") or "")
    match = _IMAGE.fullmatch(ref)
    if not match:
        return ""
    url = f"https://cdn.sanity.io/images/aqo64vfr/production/{match.group(1)}.{match.group(2)}"
    return f"![{block.get('alt') or ''}]({url})"


def _table(block: dict) -> str:
    rows = [
        [_spans_of(cell).replace("|", "\\|") for cell in row.get("cells") or []]
        for row in block.get("rows") or []
    ]
    rows = [cells for cells in rows if cells]
    if not rows:
        return ""
    rule = "| " + " | ".join("---" for _ in rows[0]) + " |"
    lines = ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join([lines[0], rule, *lines[1:]])


def _spans_of(cell: dict) -> str:
    parts = [_spans(block) for block in cell.get("content") or [] if block.get("_type") == "block"]
    return " ".join(part for part in parts if part)

"""正文抽取、图片 URL 规范化、Obsidian Markdown 写入。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify

from digest.dates import date_only, file_stamp, parse_date

_SITE_SUFFIX = re.compile(
    r"\s+(?:\||\\|[-–—])\s+(?:Hugging Face|Decagon|Sierra|Cresta|Anthropic|"
    r"Aisera|Intercom|Yellow\.ai|Cognigy|Glean|Scale AI|Harvey|Cohere|"
    r"Kimi|Zhipu|DeepSeek|Qwen|MiniMax|StepFun|LongCat|Hunyuan|Gemini|Mistral|Meta|"
    r"Databricks|Cursor|Perplexity|LangChain|DeepMind|CrewAI|Descript|Runway|Lovable|Dify|Blog)\s*$",
    re.IGNORECASE,
)
_CUT_MARKERS = re.compile(
    r"\n## (?:Related posts|Related Posts|Explore more topics|Keep reading)\b",
    re.IGNORECASE,
)
_TRAFILATURA_MD = dict(
    output_format="markdown",
    include_links=True,
    include_images=True,
    include_formatting=True,
    include_tables=True,
)


@dataclass
class Article:
    title: str
    published: datetime | None
    date_source: str
    author: str
    description: str
    body: str


def extract_article(html: str, url: str, hint_published: str = "", hint_kind: str = "") -> Article:
    soup = BeautifulSoup(html, "lxml")
    post = _blog_post(html)
    published, date_source = _published(soup, hint_published, hint_kind, post)
    title = _title(soup) or _slug_title(url)
    return Article(
        title=title,
        published=published,
        date_source=date_source,
        author=_author(soup),
        description=_description(soup),
        body=_body(html, url, post),
    )


def _published(
    soup: BeautifulSoup,
    hint_published: str,
    hint_kind: str,
    post: dict | None = None,
) -> tuple[datetime | None, str]:
    if post:
        found = parse_date(str(post.get("publishedDate") or post.get("date") or ""))
        if found:
            return found, "page"
    for finder in (_jsonld_date, _meta_date, _posted_on, _title_date):
        found = finder(soup)
        if found:
            return found, "page"
    if hint_kind == "feed":
        found = parse_date(hint_published)
        if found:
            return found, "feed"
    found = _header_date(soup)
    if found:
        return found, "page"
    found = parse_date(hint_published)
    return (found, hint_kind or "sitemap") if found else (None, "")


def _jsonld_date(soup: BeautifulSoup) -> datetime | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            found = _walk_date(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if found:
            return found
    return None


def _walk_date(node: object) -> datetime | None:
    if isinstance(node, dict):
        published = node.get("datePublished") or node.get("datepublished")
        if isinstance(published, str):
            found = parse_date(published)
            if found:
                return found
        for value in node.values():
            found = _walk_date(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_date(value)
            if found:
                return found
    return None


def _meta_date(soup: BeautifulSoup) -> datetime | None:
    for attrs in (
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"property": "og:published_time"},
        {"name": "pubdate"},
        {"name": "publish-date"},
        {"itemprop": "datePublished"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            found = parse_date(str(tag["content"]))
            if found:
                return found
    return None


def _posted_on(soup: BeautifulSoup) -> datetime | None:
    heading = soup.find("h1")
    if heading is None:
        return None
    node = heading
    for _ in range(25):
        node = node.find_next()
        if node is None or not getattr(node, "name", None):
            continue
        if node.name not in {"p", "span", "div", "time"}:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if not text or len(text) > 50 or not text.lower().startswith("posted on"):
            continue
        if text.lower() == "posted on":
            return None
        return parse_date(text)
    return None


def _title_date(soup: BeautifulSoup) -> datetime | None:
    """标题旁边的发表日期。Meta 重定向后的正文页把日期放在 h1 附近，没有 meta 标签。"""
    for heading in soup.find_all("h1"):
        for text in _near_heading(heading):
            found = parse_date(text)
            if found:
                return found
    return None


def _near_heading(heading) -> list[str]:
    texts: list[str] = []
    parent = heading.parent
    nodes = [parent, parent.parent if parent is not None else None]
    for node in nodes:
        if node is None:
            continue
        for child in list(node.children)[:8]:
            if not getattr(child, "get_text", None):
                continue
            texts.extend(_short_bits(child))
    return texts


def _short_bits(node) -> list[str]:
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    if text and len(text) <= 48:
        return [text]
    # 纯文本节点有 get_text，没有 find_all。
    if not getattr(node, "name", None):
        return []
    bits: list[str] = []
    for child in list(node.find_all(True, recursive=False))[:6]:
        bit = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
        if bit and len(bit) <= 48:
            bits.append(bit)
    return bits


def _header_date(soup: BeautifulSoup) -> datetime | None:
    heading = soup.find("h1")
    if heading is None:
        return None
    for node in heading.find_all_previous(limit=15):
        if not getattr(node, "name", None) or node.find(True):
            continue
        text = node.get_text(" ", strip=True)
        if text and len(text) <= 40:
            found = parse_date(text)
            if found:
                return found
    return None


def _title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return _clean_title(str(og["content"]))
    heading = soup.find("h1")
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return _clean_title(text)
    if soup.title and soup.title.string:
        return _clean_title(soup.title.get_text(" ", strip=True))
    return ""


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return _SITE_SUFFIX.sub("", title).strip() or title


def _slug_title(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip() or "untitled"


def _author(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "author"}, {"property": "article:author"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            author = str(tag["content"]).strip()
            if author and not author.startswith("http"):
                return author
    return ""


def _description(soup: BeautifulSoup) -> str:
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return re.sub(r"\s+", " ", str(tag["content"])).strip()
    return ""


def _body(html: str, url: str, post: dict | None = None) -> str:
    # GoDaddy Website Builder 把正文放在 window._BLOG_DATA，静态页面只剩页脚和 Cookie。
    blog = _godaddy_blog_body(post) if post else ""
    if len(blog) >= 80:
        return blog
    if "huggingface.co/blog/" in url:
        huggingface = _huggingface_body(html)
        if len(huggingface) >= 80:
            return huggingface
    if "cohere.com/blog/" in url:
        cohere = _cohere_body(html)
        if len(cohere) >= 80:
            return cohere
    if "decagon.ai/blog/" in url:
        return _decagon_body(html)
    focused = _focused_html(html, url)
    body = trafilatura.extract(focused, favor_precision=True, url=url, **_TRAFILATURA_MD) or ""
    if len(body.strip()) < 80:
        body = trafilatura.extract(focused, favor_recall=True, url=url, **_TRAFILATURA_MD) or body
    return _clean_body(body)


def _decagon_body(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select(".ra_article_careers, .ra_article_richtext_career"):
        node.decompose()
    blocks = [
        node
        for node in soup.select("div.ra_article_richtext")
        if len(node.get_text(" ", strip=True)) > 40
    ]
    blocks = [node for node in blocks if not any(parent in blocks for parent in node.parents)]
    if not blocks:
        return ""
    wrapped = "<!DOCTYPE html><html><body>" + "".join(str(node) for node in blocks) + "</body></html>"
    body = trafilatura.extract(wrapped, favor_precision=True, **_TRAFILATURA_MD) or ""
    return _clean_body(body)


def _focused_html(html: str, url: str) -> str:
    if "anthropic.com/" not in url:
        return html
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article")
    return f"<!DOCTYPE html><html><body>{article}</body></html>" if article else html


_BLOG_DATA = "window._BLOG_DATA="
_DRAFT_HEADER = {
    "header-one": "#",
    "header-two": "##",
    "header-three": "###",
    "header-four": "####",
    "header-five": "#####",
    "header-six": "######",
}
_NEXT_PUSH = re.compile(r"self\.__next_f\.push\(\[1,(.*?)\]\)\s*</script>", re.S)
_GHOST_HTML_REF = re.compile(r'ghostBlogData"\s*:\s*\{.*?"html"\s*:\s*"\$([0-9a-f]+)"')
_FLIGHT_TAIL = re.compile(r"(?<=[>])\s*[0-9a-f]+:\[")


def _blog_post(html: str) -> dict | None:
    start = html.find(_BLOG_DATA)
    if start < 0:
        return None
    blob = html[start + len(_BLOG_DATA) :]
    end = blob.find("</script>")
    blob = blob[:end] if end >= 0 else blob
    blob = blob.strip().rstrip(";")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    post = data.get("post") if isinstance(data, dict) else None
    return post if isinstance(post, dict) else None


def _godaddy_blog_body(post: dict) -> str:
    full = post.get("fullContent")
    try:
        raw = json.loads(full) if isinstance(full, str) else full
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict):
        return ""
    entities = raw.get("entityMap") or {}
    lines: list[str] = []
    for block in raw.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "unstyled")
        if kind == "atomic":
            image = _draft_image(block, entities)
            if image:
                lines.append(image)
            continue
        text = _draft_inline(block, entities).strip()
        if not text:
            continue
        prefix = _DRAFT_HEADER.get(kind)
        if prefix:
            lines.append(f"{prefix} {text}")
        elif kind == "blockquote":
            lines.append("> " + text.replace("\n", "\n> "))
        elif kind == "unordered-list-item":
            lines.append(f"- {text}")
        elif kind == "ordered-list-item":
            lines.append(f"1. {text}")
        elif kind == "code-block":
            lines.append(f"```\n{text}\n```")
        else:
            lines.append(text)
    return _clean_body("\n\n".join(lines))


def _draft_inline(block: dict, entities: dict) -> str:
    text = str(block.get("text") or "")
    if not block.get("inlineStyleRanges") and not block.get("entityRanges"):
        return text
    spans: list[tuple[int, int, str, str]] = []
    for item in block.get("inlineStyleRanges") or []:
        style = item.get("style")
        mark = {"BOLD": ("**", "**"), "ITALIC": ("*", "*"), "CODE": ("`", "`")}.get(style)
        if mark:
            spans.append((int(item.get("offset") or 0), int(item.get("length") or 0), mark[0], mark[1]))
    for item in block.get("entityRanges") or []:
        entity = entities.get(str(item.get("key"))) or {}
        url = str((entity.get("data") or {}).get("url") or "") if entity.get("type") == "LINK" else ""
        if url:
            spans.append((int(item.get("offset") or 0), int(item.get("length") or 0), "[", f"]({url})"))
    for start, length, left, right in sorted(spans, key=lambda span: (span[0], -span[1]), reverse=True):
        end = start + length
        if 0 <= start < end <= len(text):
            text = text[:start] + left + text[start:end] + right + text[end:]
    return text


def _draft_image(block: dict, entities: dict) -> str:
    for item in block.get("entityRanges") or []:
        entity = entities.get(str(item.get("key"))) or {}
        if entity.get("type") != "IMAGE":
            continue
        data = entity.get("data") or {}
        src = str(data.get("src") or "")
        if src.startswith("//"):
            src = "https:" + src
        if src:
            return f"![{data.get('alt') or ''}]({src})"
    return ""


def _cohere_body(html: str) -> str:
    """2026-03 及更早的 Cohere 文把正文放在 Ghost HTML 的 flight 文本块里，静态页面只有导语。"""
    raw = _next_flight(html)
    match = _GHOST_HTML_REF.search(raw)
    if not match:
        return ""
    fragment = _flight_text(raw, match.group(1))
    if not fragment.lstrip().startswith("<"):
        return ""
    soup = BeautifulSoup(fragment, "lxml")
    for tag in soup.find_all(["svg", "script", "style"]):
        tag.decompose()
    node = soup.body or soup
    return _clean_body(markdownify(str(node), heading_style="ATX", bullets="-"))


def _next_flight(html: str) -> str:
    parts: list[str] = []
    for match in _NEXT_PUSH.finditer(html):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, str):
            parts.append(payload)
    return "".join(parts)


def _flight_text(raw: str, row_id: str) -> str:
    match = re.search(rf"(?:^|\n){re.escape(row_id)}:T([0-9a-f]+),", raw)
    if not match:
        return ""
    text = raw[match.end() : match.end() + int(match.group(1), 16)]
    cut = _FLIGHT_TAIL.search(text)
    return text[: cut.start()] if cut else text


def _huggingface_body(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("div.blog-content")
    if root is None:
        return ""
    blocks = list(root.find_all("div", recursive=False))
    if not blocks:
        return ""
    body = max(blocks, key=lambda node: len(node.get_text()))
    for selector in (".not-prose", "svg", "button", "a.header-link", ".absolute"):
        for node in body.select(selector):
            node.decompose()
    return _clean_body(markdownify(str(body), heading_style="ATX", bullets="-"))


def _clean_body(body: str) -> str:
    body = _CUT_MARKERS.split(body, maxsplit=1)[0]
    kept = [
        line
        for line in body.splitlines()
        if not re.fullmatch(r"Posted on(?: .+)?", line.strip()) and line.strip() != "## Article"
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# 图片：保留博客原始地址（相对路径转绝对 URL，不下载）
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_IMAGE = re.compile(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'][^>]*>)', re.IGNORECASE)
_SKIP_SCHEMES = ("data:", "blob:", "javascript:", "mailto:")


def absolutize_images(body: str, page_url: str) -> str:
    """将 Markdown/HTML 中的相对图片地址解析为博客原始绝对 URL，不下载。"""

    def replace_md(match: re.Match[str]) -> str:
        alt, raw_url = match.group(1), match.group(2).strip()
        absolute = _absolute_image_url(raw_url, page_url)
        return f"![{alt}]({absolute})" if absolute else match.group(0)

    def replace_html(match: re.Match[str]) -> str:
        prefix, raw_url, suffix = match.group(1), match.group(2).strip(), match.group(3)
        absolute = _absolute_image_url(raw_url, page_url)
        return f"{prefix}{absolute}{suffix}" if absolute else match.group(0)

    return _HTML_IMAGE.sub(replace_html, _MD_IMAGE.sub(replace_md, body))


def _absolute_image_url(raw_url: str, page_url: str) -> str | None:
    if not raw_url or raw_url.startswith("#"):
        return None
    lower = raw_url.lower()
    if any(lower.startswith(scheme) for scheme in _SKIP_SCHEMES):
        return None
    absolute = urljoin(page_url, raw_url)
    if urlparse(absolute).scheme not in {"http", "https"}:
        return None
    return absolute


# 文件名与 scripts/clippings.ts sanitizeFilename 一致
_CONTROL = re.compile(r"[\u0000-\u001f\u007f\u200b-\u200f\ufeff]")
_ILLEGAL = re.compile(r'[<>:"/\\|?*#%{}$!`\'=+@&;]')
_FULLWIDTH_JUNK = re.compile(r"[\uFF1A\uFF5C\uFF0F\uFE63\uFE31\uFE32]")
_TRAILING_DOTS = re.compile(r"[.．。]+$")
_SPACES = re.compile(r"[\s\u3000]+")
_TRAILING_DOT_SPACE = re.compile(r"[.．\s]+$")


def sanitize_filename(title: str, max_len: int = 120) -> str:
    cleaned = unicodedata.normalize("NFKC", title or "")
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = _ILLEGAL.sub("", cleaned)
    cleaned = _FULLWIDTH_JUNK.sub("", cleaned)
    cleaned = _TRAILING_DOTS.sub("", cleaned)
    cleaned = _SPACES.sub(" ", cleaned).strip()
    cleaned = _TRAILING_DOT_SPACE.sub("", cleaned)[:max_len].strip()
    return _TRAILING_DOT_SPACE.sub("", cleaned) or "untitled"


def write_markdown(output_dir: Path, company: str, article: Article, url: str) -> Path:
    folder = output_dir / sanitize_filename(company, 80)
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{file_stamp(article.published)}-{sanitize_filename(article.title)}.md"
    path = _unique_path(folder, filename, url)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_render_markdown(article, company, url), encoding="utf-8")
    temporary.replace(path)
    return path


def retitle_markdown(path: Path, title: str, url: str) -> Path:
    """把笔记文件名改成中文标题，日期前缀保持不变。"""
    stamp = re.match(r"^(\d{8})-", path.name)
    prefix = f"{stamp.group(1)}-" if stamp else ""
    filename = f"{prefix}{sanitize_filename(title)}.md"
    if path.name == filename:
        _set_frontmatter_title(path, title)
        return path
    target = path.parent / filename
    if target.exists() and target.resolve() != path.resolve():
        digest = hashlib.sha1(url.encode()).hexdigest()[:6]
        target = path.parent / f"{prefix}{sanitize_filename(title)}-{digest}.md"
        if target.exists() and target.resolve() != path.resolve():
            _set_frontmatter_title(path, title)
            return path
    path.rename(target)
    _set_frontmatter_title(target, title)
    return target


def _set_frontmatter_title(path: Path, title: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    end = text.find("\n---\n", 3)
    if end == -1:
        return
    head, body = text[:end], text[end:]
    replaced, count = re.subn(r'(?m)^title:.*$', f"title: {_quote(title)}", head, count=1)
    if count:
        path.write_text(replaced + body, encoding="utf-8")


def update_markdown_body(path: Path, body: str) -> None:
    """原地改写 Obsidian 笔记正文，保留 YAML frontmatter。"""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            head = text[: end + 5]
            path.write_text(head + "\n" + body.strip() + "\n", encoding="utf-8")
            return
    path.write_text(body.strip() + "\n", encoding="utf-8")


def read_markdown_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[end + 5 :].strip()
    return text.strip()


def _render_markdown(article: Article, company: str, url: str) -> str:
    lines = [
        "---",
        f'title: {_quote(article.title)}',
        f'company: {_quote(company)}',
        f'source: {_quote(url)}',
    ]
    if article.author:
        lines.append(f"author: {_quote(article.author)}")
    if article.published:
        lines.append(f"published: {date_only(article.published)}")
    lines.append(f"created: {date.today().isoformat()}")
    if article.description:
        lines.append(f"description: {_quote(article.description)}")
    if article.date_source:
        lines.append(f"date_source: {article.date_source}")
    lines.extend(["tags:", "  - ai-company-blog", "---", "", article.body.strip(), ""])
    return "\n".join(lines)


def _unique_path(folder: Path, filename: str, url: str) -> Path:
    path = folder / filename
    if path.exists() and _cites(path, url):
        return path
    found = _find_source(folder, url)
    if found:
        return found
    if not path.exists():
        return path
    digest = hashlib.sha1(url.encode()).hexdigest()[:6]
    return folder / f"{path.stem}-{digest}.md"


def discard_alias(folder: Path, url: str, keep: Path) -> None:
    """重定向前的地址如果已经落过盘，删掉那份，只留最终文件。"""
    needle = url.split("#", 1)[0].rstrip("/")
    if not needle:
        return
    for existing in folder.glob("*.md"):
        if existing.resolve() == keep.resolve() or not _cites(existing, needle):
            continue
        existing.unlink()


def _find_source(folder: Path, url: str) -> Path | None:
    needle = url.split("#", 1)[0].rstrip("/")
    if not needle:
        return None
    for existing in folder.glob("*.md"):
        if _cites(existing, needle):
            return existing
    return None


def _cites(path: Path, url: str) -> bool:
    needle = url.split("#", 1)[0].strip().strip('"').rstrip("/")
    if not needle:
        return False
    head = path.read_text(encoding="utf-8", errors="replace")[:2500]
    match = re.search(r'(?m)^source:\s*"?(.*?)"?\s*$', head)
    if not match:
        return False
    return match.group(1).strip().strip('"').rstrip("/") == needle


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    return f'"{escaped}"'

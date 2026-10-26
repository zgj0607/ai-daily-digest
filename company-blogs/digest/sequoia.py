"""Sequoia 文章页会被 Cloudflare 403。正文在公开的 WordPress 接口里。"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from markdownify import markdownify

from digest.content import Article
from digest.dates import parse_date
from digest.http import Fetcher

_ORIGIN = "https://www.sequoia.com/blog"
_API = "https://www.sequoia.com/wp-json/wp/v2/posts"
_POST = re.compile(r"^https://www\.sequoia\.com/(\d{4})/(\d{2})/([^/]+)$")


def is_blog(index_url: str) -> bool:
    return index_url.rstrip("/") == _ORIGIN


def posts(fetcher: Fetcher) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    page = 1
    while page <= 40:
        batch, total_pages = _page(fetcher, page)
        for item in batch:
            link = str(item.get("link") or "").rstrip("/")
            if not _POST.fullmatch(link):
                continue
            title = _plain((item.get("title") or {}).get("rendered") or "")
            found.append((link, title, str(item.get("date") or "")))
        if page >= total_pages or len(batch) < 100:
            break
        page += 1
    return found


def article(fetcher: Fetcher, url: str) -> Article | None:
    match = _POST.fullmatch(url.rstrip("/"))
    if not match:
        return None
    response = fetcher.get(f"{_API}?{urlencode({'slug': match.group(3)})}")
    posts_ = response.json()
    if not isinstance(posts_, list) or not posts_:
        raise RuntimeError(f"WordPress 没有这篇文章: {url}")
    post = posts_[0]
    content = (post.get("content") or {}).get("rendered") or ""
    return Article(
        title=_plain((post.get("title") or {}).get("rendered") or ""),
        published=parse_date(str(post.get("date") or "")),
        date_source="page",
        author="",
        description=_plain((post.get("excerpt") or {}).get("rendered") or ""),
        body=_markdown(content),
    )


def _page(fetcher: Fetcher, page: int) -> tuple[list, int]:
    query = urlencode({"per_page": 100, "page": page, "_fields": "link,title,date"})
    response = fetcher.get(f"{_API}?{query}")
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"WordPress 列表不是数组: {page}")
    try:
        total_pages = int(response.headers.get("x-wp-totalpages") or 1)
    except (TypeError, ValueError):
        total_pages = 1
    return data, total_pages


def _plain(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _markdown(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    node = soup.body or soup
    text = markdownify(str(node), heading_style="ATX", bullets="-")
    return re.sub(r"\n{3,}", "\n\n", text).strip()

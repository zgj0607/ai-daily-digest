"""发现 URL → 并行爬取落盘 → 并行读 Obsidian 翻译（爬取与翻译流水线重叠）。"""
from __future__ import annotations

import gzip
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Queue
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from digest.config import Settings, Source
from digest.content import (
    absolutize_images,
    discard_alias,
    extract_article,
    read_markdown_body,
    retitle_markdown,
    update_markdown_body,
    write_markdown,
)
from digest.dates import parse_date, to_iso
from digest.http import Fetcher
from digest import perplexity
from digest.state import ArticleRow, State
from digest.translate import Translator, configure_llm_concurrency
from digest.ui import board


@dataclass
class SourceReport:
    source_id: str
    name: str
    discovered: int
    saved: int
    failed: int
    error: str = ""


@dataclass
class SavedArticle:
    source_id: str
    title: str
    url: str
    filepath: Path


@dataclass
class _Item:
    url: str
    title: str = ""
    published: str = ""
    hint_kind: str = ""
    lastmod: str = ""


_TRANSLATE_STOP = object()


class _View:
    """固定在顶部的整体进展。交互终端里由面板重绘，否则逐行打印。"""

    def __init__(self, pipeline: str) -> None:
        self.pipeline = pipeline
        self.plan_lines = ["正在发现文章链接…"]
        self.progress = "爬取 — | 翻译 —"
        self.discover = "发现 0/0"
        self.discover_done = 0
        self.discover_total = 0
        self._lock = threading.Lock()

    def _lines(self) -> list[str]:
        return [self.pipeline, *self.plan_lines, self.progress, self.discover]

    def _publish(self, echo: list[str] | None = None) -> None:
        with self._lock:
            lines = self._lines()
        board.set_header(lines)
        if echo and not board.live:
            for line in echo:
                print(line, flush=True)

    def refresh(self) -> None:
        self._publish()

    def set_plan(self, *lines: str) -> None:
        with self._lock:
            self.plan_lines = list(lines) or [""]
        self._publish(list(lines))

    def set_progress(self, text: str) -> None:
        with self._lock:
            self.progress = text
        self._publish([text])

    def discover_begin(self, name: str) -> None:
        with self._lock:
            self.discover = f"发现 {self.discover_done}/{self.discover_total} · 正在查看 {name}"
        self._publish([f"开始发现 {name}"])

    def discover_end(self, name: str, count: int, error: str) -> None:
        with self._lock:
            self.discover_done += 1
            detail = f"{name} 失败 {error}" if error else f"{name} {count} 篇"
            self.discover = f"发现 {self.discover_done}/{self.discover_total} · {detail}"
        message = f"[{name}] 发现失败 {error}" if error else f"[{name}] 发现 {count} 篇"
        self._publish([message])


class _Progress:
    """爬取与翻译的本次总量和完成数。"""

    def __init__(self, view: _View) -> None:
        self.view = view
        self.ready = False
        self.crawl_total = 0
        self.crawl_done = 0
        self.crawl_ok = 0
        self.translate_total = 0
        self.translate_done = 0
        self.translate_ok = 0
        self._lock = threading.Lock()

    def arm(self, crawl_total: int, translate_total: int) -> None:
        with self._lock:
            self.crawl_total = crawl_total
            self.translate_total = translate_total
            self.ready = True
            self._emit()

    def note_crawl(self, ok: bool) -> None:
        with self._lock:
            self.crawl_done += 1
            if ok:
                self.crawl_ok += 1
            elif self.translate_total > self.translate_done:
                self.translate_total -= 1
            self._emit()

    def note_translate(self, ok: bool) -> None:
        with self._lock:
            self.translate_done += 1
            if ok:
                self.translate_ok += 1
            self._emit()

    def _emit(self) -> None:
        if not self.ready:
            return
        crawl_fail = self.crawl_done - self.crawl_ok
        translate_fail = self.translate_done - self.translate_ok
        self.view.set_progress(
            f"进度 爬取 {self.crawl_done}/{self.crawl_total}"
            f"（成功 {self.crawl_ok}，失败 {crawl_fail}）"
            f" | 翻译 {self.translate_done}/{self.translate_total}"
            f"（成功 {self.translate_ok}，失败 {translate_fail}）"
        )


def run(
    settings: Settings,
    only: str | None = None,
    limit: int | None = None,
) -> int:
    sources = settings.enabled_sources(only)
    state = State(settings.state_db)
    fetcher = Fetcher(settings.user_agent, settings.request_interval_seconds)
    configure_llm_concurrency(settings.llm_concurrency)
    translator = Translator.from_env()
    crawl_workers = settings.article_concurrency
    translate_workers = max(1, translator.slots)
    pipeline = (
        f"流水线: 发现={settings.source_concurrency}, "
        f"爬取={crawl_workers}, 翻译线程={translate_workers}"
        f"（本地同时 {settings.llm_concurrency} 篇并按 8096 token 切段，云端 Coding Plan 从 8 篇加到最多 20 篇）"
    )
    print(f"[digest] {pipeline}")
    view = _View(pipeline)
    progress = _Progress(view)

    reports = {s.id: SourceReport(s.id, s.name, 0, 0, 0) for s in sources}
    sources_by_id = {s.id: s for s in settings.sources}
    translate_q: Queue[SavedArticle | object] = Queue()
    translated_lock = threading.Lock()
    translated_ok = 0
    translated_fail = 0

    def translate_loop() -> None:
        nonlocal translated_ok, translated_fail
        while True:
            item = translate_q.get()
            try:
                if item is _TRANSLATE_STOP:
                    return
                assert isinstance(item, SavedArticle)
                try:
                    _translate_saved(item, translator, state, settings, fetcher, sources_by_id)
                    with translated_lock:
                        translated_ok += 1
                    board.log_translate(f"[{item.source_id}] 已翻译 {item.filepath.name}")
                    progress.note_translate(True)
                except Exception as exc:
                    with translated_lock:
                        translated_fail += 1
                    board.log_translate(f"[{item.source_id}] 翻译失败（保留原文）{item.filepath.name}: {exc}")
                    progress.note_translate(False)
            finally:
                translate_q.task_done()

    translate_pool = ThreadPoolExecutor(
        max_workers=translate_workers, thread_name_prefix="translate"
    )
    for _ in range(translate_workers):
        translate_pool.submit(translate_loop)

    view.discover_total = len(sources)
    view.refresh()
    board.start()
    try:
        # 上次没译完的笔记全部进队列，和本次新文一起翻译
        backlog = state.needs_translate()
        for row in backlog:
            translate_q.put(
                SavedArticle(
                    source_id=row.source_id,
                    title=row.title,
                    url=row.url,
                    filepath=Path(row.filepath),
                )
            )

        with ThreadPoolExecutor(max_workers=settings.source_concurrency) as pool:
            futures = {
                pool.submit(_discover_source, state, fetcher, source, view): source for source in sources
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    count, error = future.result()
                    reports[source.id].discovered = count
                    reports[source.id].error = error
                except Exception as exc:
                    reports[source.id].error = str(exc)
                    view.discover_end(source.name, 0, str(exc))

        jobs: list[tuple[Source, ArticleRow]] = []
        for source in sources:
            if reports[source.id].error:
                continue
            for row in state.pending(source.id, settings.max_attempts, limit):
                if not _article_url(row.url):
                    state.skip(source.id, row.url, "不是文章链接")
                elif source.accepts(row.url):
                    jobs.append((source, row))
                else:
                    state.skip(source.id, row.url, "短链接仅为重定向，改抓规范地址")

        saved_n, translated_n, _ = state.library_counts([s.id for s in sources])
        need_crawl = len(jobs)
        need_translate = len(backlog) + need_crawl
        backlog_note = f"未译积压 {len(backlog)} 篇"
        limit_note = f"，每源上限 {limit} 篇" if limit is not None else ""
        view.set_plan(
            f"数据库比对: 已爬取 {saved_n} 篇，已翻译 {translated_n} 篇",
            f"本次需要爬取 {need_crawl} 篇{limit_note}，需要翻译 {need_translate} 篇"
            f"（{backlog_note}，新爬取 {need_crawl} 篇落盘后翻译）",
        )
        progress.arm(need_crawl, need_translate)
        if jobs:
            with ThreadPoolExecutor(max_workers=crawl_workers, thread_name_prefix="crawl") as pool:
                futures = {
                    pool.submit(_crawl_article, settings, state, fetcher, source, row): (source, row)
                    for source, row in jobs
                }
                for future in as_completed(futures):
                    source, row = futures[future]
                    try:
                        ok, message, saved = future.result()
                    except Exception as exc:
                        ok, message, saved = False, str(exc), None
                        state.mark_failed(source.id, row.url, message)
                    if ok and saved:
                        reports[source.id].saved += 1
                        translate_q.put(saved)
                        board.log_crawl(f"[{source.id}] 已落盘 {message}")
                        progress.note_crawl(True)
                        continue
                    reports[source.id].failed += 1
                    board.log_crawl(f"[{source.id}] 爬取失败 {row.url}: {message}")
                    progress.note_crawl(False)

        for source in sources:
            if not reports[source.id].error:
                state.finish_source(source.id, bootstrapped=limit is None, error="")
    finally:
        view.set_plan("等待翻译队列清空…")
        translate_q.join()
        for _ in range(translate_workers):
            translate_q.put(_TRANSLATE_STOP)
        translate_pool.shutdown(wait=True)
        board.stop()
        fetcher.close()
        state.close()

    items = [reports[s.id] for s in sources]
    saved = sum(i.saved for i in items)
    failed = sum(i.failed for i in items)
    for item in items:
        if item.error:
            print(f"{item.name}: 发现失败 {item.error}")
        else:
            print(f"{item.name}: 发现 {item.discovered}，本次落盘 {item.saved}，失败 {item.failed}")
    print(f"合计: 落盘 {saved}，爬取失败 {failed}，翻译成功 {translated_ok}，翻译失败 {translated_fail}")
    return 1 if items and all(i.error for i in items) else 0


def show_status(settings: Settings) -> int:
    state = State(settings.state_db)
    try:
        rows = state.summary()
    finally:
        state.close()
    if not rows:
        print("还没有爬取记录。")
        return 0
    for row in rows:
        stage = "已完成首次全量" if row["bootstrapped"] else "首次全量未完成"
        err = f"，上次错误: {row['last_error']}" if row["last_error"] else ""
        untranslated = row["untranslated"] or 0
        print(
            f"{row['id']}: {stage}，水位 {row['watermark'] or '-'}，"
            f"已保存 {row['saved'] or 0}，待抓 {row['pending'] or 0}，失败 {row['failed'] or 0}，"
            f"待译 {untranslated}，上次运行 {row['last_run_at'] or '-'}{err}"
        )
    return 0


def _discover_source(state: State, fetcher: Fetcher, source: Source, view: _View) -> tuple[int, str]:
    state.ensure_source(source.id)
    view.discover_begin(source.name)
    try:
        found = _discover(source, fetcher)
    except Exception as exc:
        state.finish_source(source.id, bootstrapped=False, error=str(exc))
        view.discover_end(source.name, 0, str(exc))
        return 0, str(exc)

    for item in found.values():
        state.note_discovered(
            source.id,
            item.url,
            title=item.title,
            published=item.published or item.lastmod,
            hint_kind=item.hint_kind or ("sitemap" if item.lastmod else ""),
        )
    state.commit()
    view.discover_end(source.name, len(found), "")
    return len(found), ""


def _crawl_article(
    settings: Settings,
    state: State,
    fetcher: Fetcher,
    source: Source,
    row: ArticleRow,
) -> tuple[bool, str, SavedArticle | None]:
    """只负责抓取并写入 Obsidian 原文，不翻译。"""
    try:
        article = perplexity.article(fetcher, row.url)
        if article is None:
            html, page_url = fetcher.text_resolved(row.url)
            article = extract_article(html, page_url, row.published, row.hint_kind)
        else:
            page_url = row.url
        page_url = _normalize(page_url)
        if len(article.body) < 80:
            raise RuntimeError("正文过短")
        if not article.title:
            article.title = row.title or article.title
        article.body = absolutize_images(article.body, page_url)
        path = write_markdown(settings.output_dir, source.name, article, page_url)
        if _normalize(row.url) != page_url:
            discard_alias(path.parent, row.url, path)
        published = to_iso(article.published)
        state.mark_saved(source.id, row.url, article.title, published, str(path))
        if page_url != row.url:
            state.mark_saved(source.id, page_url, article.title, published, str(path))
            state.mark_translated(source.id, page_url, title=article.title, filepath=str(path))
        saved = SavedArticle(
            source_id=source.id,
            title=article.title,
            url=row.url,
            filepath=path,
        )
        return True, f"{path.parent.name}/{path.name}", saved
    except Exception as exc:
        state.mark_failed(source.id, row.url, str(exc))
        return False, str(exc), None


def _recrawl_missing(
    article: SavedArticle,
    settings: Settings,
    state: State,
    fetcher: Fetcher,
    sources: dict[str, Source],
) -> SavedArticle:
    """原文文件已不在时，按记录中的公司目录和原文地址重新爬取。"""
    if not article.url:
        raise RuntimeError(f"文件不存在且没有原文地址: {article.filepath}")
    company = article.filepath.parent.name or article.source_id
    known = sources.get(article.source_id)
    source = replace(known, name=company) if known else Source(article.source_id, company, re.compile(".*"))
    row = ArticleRow(article.source_id, article.url, article.title, "", "", "saved", 0, "", "")
    ok, message, saved = _crawl_article(settings, state, fetcher, source, row)
    if not ok or saved is None:
        raise RuntimeError(message or "重新爬取失败")
    return saved


def _translate_saved(
    article: SavedArticle,
    translator: Translator,
    state: State,
    settings: Settings,
    fetcher: Fetcher,
    sources: dict[str, Source],
) -> None:
    """读取已落盘的 Obsidian 文件，双语翻译后写回，并把文件名改成中文标题。"""
    path = article.filepath
    if not path.is_file():
        board.log_translate(f"[{article.source_id}] 原文不存在，重新爬取 {path.parent.name}/{article.url}")
        refetched = _recrawl_missing(article, settings, state, fetcher, sources)
        article.title = refetched.title
        article.filepath = refetched.filepath
        path = article.filepath
    body = read_markdown_body(path)
    title_zh = article.title
    if len(body) < 40:
        title_zh = translator.translate_title(article.title)
    else:
        try:
            title_zh, bilingual = translator.add_bilingual(body, article.title)
        except Exception as exc:
            # 翻译失败保留原文，仍标记为已处理以免死循环；needs_translate 不再捡起
            board.log_translate(f"[{article.source_id}] 翻译异常，保留原文: {exc}")
            state.mark_translated(article.source_id, article.url)
            return
        if bilingual.strip() != body.strip():
            update_markdown_body(path, bilingual)
    path = retitle_markdown(path, title_zh or article.title, article.url)
    article.title = title_zh or article.title
    article.filepath = path
    state.mark_translated(article.source_id, article.url, title=article.title, filepath=str(path))


def _discover(source: Source, fetcher: Fetcher) -> dict[str, _Item]:
    if perplexity.is_blog(source.index_url):
        return {
            url: _Item(url=url, title=title, published=to_iso(parse_date(published)) if published else "", hint_kind="page")
            for url, title, published in perplexity.posts(fetcher)
        }
    items: dict[str, _Item] = {}

    def accept(raw: str) -> str:
        url = _normalize(raw)
        return url if _article_url(url) and source.accepts(url) else ""

    if source.sitemap_url:
        for url, lastmod in _sitemap_urls(fetcher, source.sitemap_url, 0):
            if url := accept(url):
                items[url] = _Item(url=url, lastmod=to_iso(parse_date(lastmod)) if lastmod else "")
    if source.index_url:
        index_path = urlparse(source.index_url).path.rstrip("/")
        links, dated = _index_links(fetcher, source.index_url)
        for url in links:
            url = accept(url)
            if not url or urlparse(url).path.rstrip("/") == index_path:
                continue
            published = dated.get(url, "")
            if url not in items:
                items[url] = _Item(url=url, published=published, hint_kind="index")
            elif published and not items[url].published:
                items[url].published = published
    if source.feed_url:
        for url, title, published in _feed_entries(fetcher.bytes(source.feed_url)):
            if not (url := accept(url)):
                continue
            item = items.get(url) or _Item(url=url)
            item.title = title or item.title
            item.published = published or item.published
            item.hint_kind = "feed"
            items[url] = item
    return items


_HREF = re.compile(r"""href=["']([^"'#]+)""", re.I)
_NEXT_HREF = re.compile(
    r"""<a\b[^>]*href=["']([^"']+)["'][^>]*\brel=["']next["']"""
    r"""|<a\b[^>]*\brel=["']next["'][^>]*href=["']([^"']+)["']""",
    re.I,
)


def _index_links(fetcher: Fetcher, page_url: str) -> tuple[list[str], dict[str, str]]:
    """从列表页抽出文章链接和卡片上的发表日期。同一路径上的 ?page= 会继续翻页。"""
    seen_pages: set[str] = set()
    seen: dict[str, None] = {}
    dated: dict[str, str] = {}
    current = page_url
    for _ in range(80):
        if current in seen_pages:
            break
        seen_pages.add(current)
        html = fetcher.text(current)
        for href in _HREF.findall(html):
            seen.setdefault(urljoin(current, href.strip()), None)
        for anchor in BeautifulSoup(html, "lxml").find_all("a", href=True):
            raw = urljoin(current, anchor["href"].strip())
            url = _normalize(raw)
            if url in dated:
                continue
            when = parse_date(_card_date(anchor))
            if when:
                dated[url] = to_iso(when)
        nxt = _next_index(html, current)
        if not nxt:
            break
        current = nxt
    return list(seen), dated


def _card_date(anchor) -> str:
    """列表卡片里、标题旁边的日期。不要爬到包含多篇文章的外层。"""
    node = anchor
    for _ in range(5):
        parent = getattr(node, "parent", None)
        if parent is None or not getattr(parent, "find_all", None):
            break
        slugs = set()
        for link in parent.find_all("a", href=True):
            href = link["href"].split("?")[0].rstrip("/")
            if "/blog/" in href and not href.endswith("/blog"):
                slugs.add(href)
        if len(slugs) > 1:
            break
        for child in list(parent.children)[:8]:
            if not getattr(child, "get_text", None):
                continue
            text = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
            if text and len(text) <= 32 and parse_date(text):
                return text
        node = parent
    return ""


def _next_index(html: str, page_url: str) -> str:
    match = _NEXT_HREF.search(html)
    if not match:
        return ""
    href = urljoin(page_url, (match.group(1) or match.group(2)).strip())
    same_path = urlparse(href).path.rstrip("/") == urlparse(page_url).path.rstrip("/")
    if not same_path or "page=" not in urlparse(href).query:
        return ""
    return href


def _article_url(url: str) -> bool:
    """丢掉 javascript:void(0) 这类被拼进博客域名的假链接。"""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and "javascript" not in url.lower()


def _normalize(url: str) -> str:
    url = url.strip().split("#", 1)[0]
    if url.endswith("/") and url.count("/") > 3:
        url = url.rstrip("/")
    return url


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _sitemap_urls(fetcher: Fetcher, url: str, depth: int) -> list[tuple[str, str]]:
    if depth > 3:
        return []
    try:
        root = ET.fromstring(_strip_bom(fetcher.bytes(url)))
    except (ET.ParseError, RuntimeError) as exc:
        if depth == 0:
            raise
        print(f"[digest] 跳过无法解析的 sitemap {url}: {exc}")
        return []
    if _local(root.tag) == "sitemapindex":
        found: list[tuple[str, str]] = []
        for node in root:
            if _local(node.tag) != "sitemap":
                continue
            loc = _child_text(node, "loc")
            if loc:
                found.extend(_sitemap_urls(fetcher, loc, depth + 1))
        return found
    return [
        (loc, _child_text(node, "lastmod"))
        for node in root
        if _local(node.tag) == "url" and (loc := _child_text(node, "loc"))
    ]


def _feed_entries(payload: bytes) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for node in ET.fromstring(_strip_bom(payload)).iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        url = title = published = ""
        for child in node:
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                title = re.sub(r"\s+", " ", text).strip()
            elif name == "link":
                href = (child.attrib.get("href") or text).strip()
                if href and not url:
                    url = href
            elif name in {"pubDate", "published", "updated", "date"} and text and not published:
                published = to_iso(parse_date(text))
            elif name == "guid" and text.startswith("http") and not url:
                url = text
        if url:
            entries.append((url, title, published))
    return entries


def _strip_bom(payload: bytes) -> bytes:
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    return payload[3:] if payload.startswith(b"\xef\xbb\xbf") else payload

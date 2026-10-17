"""发现 URL → 并行爬取落盘 → 并行读 Obsidian 翻译（爬取与翻译流水线重叠）。"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from queue import Queue
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from digest.config import Settings, Source
from digest.content import (
    absolutize_images,
    extract_article,
    read_markdown_body,
    update_markdown_body,
    write_markdown,
)
from digest.dates import parse_date, to_iso
from digest.http import Fetcher
from digest.insight import enrich_insights
from digest.report import SavedArticle, SourceReport, excerpt_from_body, write_blog_report
from digest.state import ArticleRow, State
from digest.translate import Translator, configure_llm_concurrency


@dataclass
class _Item:
    url: str
    title: str = ""
    published: str = ""
    hint_kind: str = ""
    lastmod: str = ""


_TRANSLATE_STOP = object()


class _Progress:
    """爬取与翻译的本次总量和完成数。发现结束前只计数，之后每变一次打印一行。"""

    def __init__(self) -> None:
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
        print(
            f"[digest] 进度 爬取 {self.crawl_done}/{self.crawl_total}"
            f"（成功 {self.crawl_ok}，失败 {crawl_fail}）"
            f" | 翻译 {self.translate_done}/{self.translate_total}"
            f"（成功 {self.translate_ok}，失败 {translate_fail}）",
            flush=True,
        )


def run(
    settings: Settings,
    only: str | None = None,
    limit: int | None = None,
    summary_path: Path | None = None,
) -> int:
    sources = settings.enabled_sources(only)
    source_by_id = {s.id: s for s in sources}
    state = State(settings.state_db)
    fetcher = Fetcher(settings.user_agent, settings.request_interval_seconds)
    translator = Translator.from_env()
    configure_llm_concurrency(settings.llm_concurrency)
    crawl_workers = settings.article_concurrency
    translate_workers = settings.llm_concurrency
    print(
        f"[digest] 流水线: 发现={settings.source_concurrency}, "
        f"爬取={crawl_workers}, 翻译={translate_workers}, llm={settings.llm_concurrency}"
    )

    reports = {s.id: SourceReport(s.id, s.name, 0, 0, 0) for s in sources}
    this_run: list[SavedArticle] = []
    translate_q: Queue[SavedArticle | object] = Queue()
    translated_lock = threading.Lock()
    translated_ok = 0
    translated_fail = 0
    progress = _Progress()

    def translate_loop() -> None:
        nonlocal translated_ok, translated_fail
        while True:
            item = translate_q.get()
            try:
                if item is _TRANSLATE_STOP:
                    return
                assert isinstance(item, SavedArticle)
                try:
                    _translate_saved(item, translator, state)
                    with translated_lock:
                        translated_ok += 1
                    print(f"[{item.source_id}] 已翻译 {item.filepath.name}", flush=True)
                    progress.note_translate(True)
                except Exception as exc:
                    with translated_lock:
                        translated_fail += 1
                    print(f"[{item.source_id}] 翻译失败（保留原文）{item.filepath.name}: {exc}", flush=True)
                    progress.note_translate(False)
            finally:
                translate_q.task_done()

    translate_pool = ThreadPoolExecutor(
        max_workers=translate_workers, thread_name_prefix="translate"
    )
    for _ in range(translate_workers):
        translate_pool.submit(translate_loop)

    try:
        # 先续译历史未完成笔记（有上限，避免拖垮本次关机窗口）
        backlog = state.needs_translate(limit=50)
        for row in backlog:
                source = source_by_id.get(row.source_id)
                name = source.name if source else row.source_id
                translate_q.put(
                    SavedArticle(
                        source_id=row.source_id,
                        source_name=name,
                        title=row.title,
                        url=row.url,
                        published=row.published,
                        description="",
                        filepath=Path(row.filepath),
                    )
                )

        with ThreadPoolExecutor(max_workers=settings.source_concurrency) as pool:
            futures = {pool.submit(_discover_source, state, fetcher, source): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    count, error = future.result()
                    reports[source.id].discovered = count
                    reports[source.id].error = error
                    print(f"[{source.id}] 发现失败 {error}" if error else f"[{source.id}] 发现 {count} 篇")
                except Exception as exc:
                    reports[source.id].error = str(exc)
                    print(f"[{source.id}] 发现失败 {exc}")

        jobs: list[tuple[Source, ArticleRow]] = []
        for source in sources:
            if reports[source.id].error:
                continue
            jobs.extend((source, row) for row in state.pending(source.id, settings.max_attempts, limit))

        saved_n, translated_n, untranslated_n = state.library_counts([s.id for s in sources])
        need_crawl = len(jobs)
        need_translate = len(backlog) + need_crawl
        backlog_note = f"未译积压纳入 {len(backlog)} 篇"
        if untranslated_n > len(backlog):
            backlog_note += f"（库内未译 {untranslated_n} 篇，本次最多续译 {len(backlog)} 篇）"
        limit_note = f"，每源上限 {limit} 篇" if limit is not None else ""
        print(
            f"[digest] 数据库比对: 已爬取 {saved_n} 篇，已翻译 {translated_n} 篇",
            flush=True,
        )
        print(
            f"[digest] 本次需要爬取 {need_crawl} 篇{limit_note}，需要翻译 {need_translate} 篇"
            f"（{backlog_note}，新爬取 {need_crawl} 篇落盘后翻译）",
            flush=True,
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
                        if ok and saved:
                            reports[source.id].saved += 1
                            this_run.append(saved)
                            translate_q.put(saved)
                            print(f"[{source.id}] 已落盘 {message}", flush=True)
                            progress.note_crawl(True)
                        else:
                            reports[source.id].failed += 1
                            print(f"[{source.id}] 爬取失败 {row.url}: {message}", flush=True)
                            progress.note_crawl(False)
                    except Exception as exc:
                        reports[source.id].failed += 1
                        state.mark_failed(source.id, row.url, str(exc))
                        print(f"[{source.id}] 爬取失败 {row.url}: {exc}", flush=True)
                        progress.note_crawl(False)

        for source in sources:
            if not reports[source.id].error:
                state.finish_source(source.id, bootstrapped=limit is None, error="")
    finally:
        print("[digest] 等待翻译队列清空…")
        translate_q.join()
        for _ in range(translate_workers):
            translate_q.put(_TRANSLATE_STOP)
        translate_pool.shutdown(wait=True)
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

    highlights = ""
    if this_run:
        highlights = enrich_insights(
            this_run, translator, concurrency=settings.llm_concurrency
        )

    report_path = settings.output_dir / f"{date.today().strftime('%Y%m%d')}-AI公司博客简报.md"
    write_blog_report(
        report_path,
        items,
        this_run,
        source_total=len(sources),
        highlights=highlights,
    )
    print(f"[digest] 博客简报已写入 {report_path}")
    if summary_path is not None and summary_path.resolve() != report_path.resolve():
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[digest] 简报副本已写入 {summary_path}")
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


def _discover_source(state: State, fetcher: Fetcher, source: Source) -> tuple[int, str]:
    state.ensure_source(source.id)
    print(f"开始发现 {source.name}")
    try:
        found = _discover(source, fetcher)
    except Exception as exc:
        state.finish_source(source.id, bootstrapped=False, error=str(exc))
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
        html = fetcher.text(row.url)
        article = extract_article(html, row.url, row.published, row.hint_kind)
        if len(article.body) < 80:
            raise RuntimeError("正文过短")
        if not article.title:
            article.title = row.title or article.title
        description = excerpt_from_body(article.body, article.description)
        article.body = absolutize_images(article.body, row.url)
        path = write_markdown(settings.output_dir, source.name, article, row.url)
        published = to_iso(article.published)
        state.mark_saved(source.id, row.url, article.title, published, str(path))
        saved = SavedArticle(
            source_id=source.id,
            source_name=source.name,
            title=article.title,
            url=row.url,
            published=published,
            description=description,
            filepath=path,
        )
        return True, f"{path.parent.name}/{path.name}", saved
    except Exception as exc:
        state.mark_failed(source.id, row.url, str(exc))
        return False, str(exc), None


def _translate_saved(article: SavedArticle, translator: Translator, state: State) -> None:
    """读取已落盘的 Obsidian 文件，双语翻译后写回。"""
    path = article.filepath
    if not path.is_file():
        raise RuntimeError(f"文件不存在: {path}")
    body = read_markdown_body(path)
    if len(body) < 40:
        state.mark_translated(article.source_id, article.url)
        return
    try:
        bilingual = translator.add_bilingual(body)
    except Exception as exc:
        # 翻译失败保留原文，仍标记为已处理以免死循环；needs_translate 不再捡起
        print(f"[{article.source_id}] 翻译异常，保留原文: {exc}")
        state.mark_translated(article.source_id, article.url)
        return
    if bilingual.strip() != body.strip():
        update_markdown_body(path, bilingual)
    state.mark_translated(article.source_id, article.url)


def _discover(source: Source, fetcher: Fetcher) -> dict[str, _Item]:
    items: dict[str, _Item] = {}
    if source.sitemap_url:
        for url, lastmod in _sitemap_urls(fetcher, source.sitemap_url, 0):
            url = _normalize(url)
            if source.accepts(url):
                items[url] = _Item(url=url, lastmod=to_iso(parse_date(lastmod)) if lastmod else "")
    if source.index_url:
        for url in _index_links(fetcher, source.index_url):
            url = _normalize(url)
            if source.accepts(url) and url not in items:
                items[url] = _Item(url=url, hint_kind="index")
    if source.feed_url:
        for url, title, published in _feed_entries(fetcher.bytes(source.feed_url)):
            url = _normalize(url)
            if not source.accepts(url):
                continue
            item = items.get(url) or _Item(url=url)
            item.title = title or item.title
            item.published = published or item.published
            item.hint_kind = "feed"
            items[url] = item
    return items


_HREF = re.compile(r"""href=["']([^"'#]+)""", re.I)


def _index_links(fetcher: Fetcher, page_url: str) -> list[str]:
    """从列表页抽出文章链接（sitemap 未收录时的补充）。"""
    html = fetcher.text(page_url)
    found: list[str] = []
    seen: set[str] = set()
    for href in _HREF.findall(html):
        absolute = urljoin(page_url, href.strip())
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found


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
    root = ET.fromstring(_strip_bom(fetcher.bytes(url)))
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
        import gzip

        payload = gzip.decompress(payload)
    return payload[3:] if payload.startswith(b"\xef\xbb\xbf") else payload

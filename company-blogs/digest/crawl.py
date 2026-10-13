"""发现 URL、并行抓取/翻译/落盘。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from digest.config import Settings, Source
from digest.content import extract_article, localize_images, write_markdown
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


def run(
    settings: Settings,
    only: str | None = None,
    limit: int | None = None,
    summary_path: Path | None = None,
) -> int:
    sources = settings.enabled_sources(only)
    state = State(settings.state_db)
    fetcher = Fetcher(settings.user_agent, settings.request_interval_seconds)
    translator = Translator.from_env()
    configure_llm_concurrency(settings.llm_concurrency)
    print(
        f"[digest] 并行: source={settings.source_concurrency}, "
        f"article={settings.article_concurrency}, llm={settings.llm_concurrency}"
    )

    reports = {s.id: SourceReport(s.id, s.name, 0, 0, 0) for s in sources}
    saved_articles: list[SavedArticle] = []
    try:
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

        print(f"[digest] 待处理 {len(jobs)} 篇（并发 {settings.article_concurrency}）")
        if jobs:
            with ThreadPoolExecutor(max_workers=settings.article_concurrency) as pool:
                futures = {
                    pool.submit(_process_article, settings, state, fetcher, translator, source, row): (source, row)
                    for source, row in jobs
                }
                for future in as_completed(futures):
                    source, row = futures[future]
                    try:
                        ok, message, saved = future.result()
                        if ok:
                            reports[source.id].saved += 1
                            if saved:
                                saved_articles.append(saved)
                            print(f"[{source.id}] 保存 {message}")
                        else:
                            reports[source.id].failed += 1
                            print(f"[{source.id}] 失败 {row.url}: {message}")
                    except Exception as exc:
                        reports[source.id].failed += 1
                        state.mark_failed(source.id, row.url, str(exc))
                        print(f"[{source.id}] 失败 {row.url}: {exc}")

        for source in sources:
            if not reports[source.id].error:
                state.finish_source(source.id, bootstrapped=limit is None, error="")
    finally:
        fetcher.close()
        state.close()

    items = [reports[s.id] for s in sources]
    saved = sum(i.saved for i in items)
    failed = sum(i.failed for i in items)
    for item in items:
        if item.error:
            print(f"{item.name}: 发现失败 {item.error}")
        else:
            print(f"{item.name}: 发现 {item.discovered}，本次保存 {item.saved}，失败 {item.failed}")
    print(f"合计: 保存 {saved}，失败 {failed}")

    highlights = ""
    if saved_articles:
        # 简报提炼仍用本地模型（与翻译同一套 LLM），需在关机前完成
        highlights = enrich_insights(
            saved_articles, translator, concurrency=settings.llm_concurrency
        )

    report_path = settings.output_dir / f"{date.today().strftime('%Y%m%d')}-公司博客简报.md"
    write_blog_report(
        report_path,
        items,
        saved_articles,
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
        print(
            f"{row['id']}: {stage}，水位 {row['watermark'] or '-'}，"
            f"已保存 {row['saved'] or 0}，待抓 {row['pending'] or 0}，失败 {row['failed'] or 0}，"
            f"上次运行 {row['last_run_at'] or '-'}{err}"
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


def _process_article(
    settings: Settings,
    state: State,
    fetcher: Fetcher,
    translator: Translator,
    source: Source,
    row: ArticleRow,
) -> tuple[bool, str, SavedArticle | None]:
    try:
        html = fetcher.text(row.url)
        article = extract_article(html, row.url, row.published, row.hint_kind)
        if len(article.body) < 80:
            raise RuntimeError("正文过短")
        if not article.title:
            article.title = row.title or article.title
        original_body = article.body
        description = excerpt_from_body(original_body, article.description)
        try:
            article.body = translator.add_bilingual(article.body)
        except Exception as exc:
            # 翻译失败仍落盘原文，避免整篇丢失
            print(f"[{source.id}] 翻译失败，保存原文: {exc}")
            article.body = original_body
        article.body = localize_images(
            article.body, row.url, settings.assets_dir, fetcher, source.id
        )
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


# ---------------------------------------------------------------------------
# Discovery (sitemap + feed)
# ---------------------------------------------------------------------------

def _discover(source: Source, fetcher: Fetcher) -> dict[str, _Item]:
    items: dict[str, _Item] = {}
    if source.sitemap_url:
        for url, lastmod in _sitemap_urls(fetcher, source.sitemap_url, 0):
            url = _normalize(url)
            if source.accepts(url):
                items[url] = _Item(url=url, lastmod=to_iso(parse_date(lastmod)) if lastmod else "")
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


def _normalize(url: str) -> str:
    url = url.strip().split("#", 1)[0]
    if url.endswith("/") and url.count("/") > 3:
        url = url.rstrip("/")
    return url


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sitemap_urls(fetcher: Fetcher, url: str, depth: int) -> list[tuple[str, str]]:
    if depth > 3:
        return []
    root = ET.fromstring(_strip_bom(fetcher.bytes(url)))
    if _local(root.tag) == "sitemapindex":
        found: list[tuple[str, str]] = []
        for node in root:
            if _local(node.tag) != "sitemap":
                continue
            loc = next((c.text.strip() for c in node if _local(c.tag) == "loc" and c.text), "")
            if loc:
                found.extend(_sitemap_urls(fetcher, loc, depth + 1))
        return found
    found = []
    for node in root:
        if _local(node.tag) != "url":
            continue
        loc = lastmod = ""
        for child in node:
            name = _local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if loc:
            found.append((loc, lastmod))
    return found


def _feed_entries(payload: bytes) -> list[tuple[str, str, str]]:
    root = ET.fromstring(_strip_bom(payload))
    entries: list[tuple[str, str, str]] = []
    for node in root.iter():
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
    return payload[3:] if payload.startswith(b"\xef\xbb\xbf") else payload

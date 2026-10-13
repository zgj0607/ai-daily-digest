"""本次爬取的公司博客简报（格式对齐 RSS 日报）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


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
    source_name: str
    title: str
    url: str
    published: str
    description: str
    filepath: Path


def write_blog_report(
    path: Path,
    items: list[SourceReport],
    articles: list[SavedArticle],
    *,
    source_total: int,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        generate_blog_report(items, articles, source_total=source_total),
        encoding="utf-8",
    )
    return path


def generate_blog_report(
    items: list[SourceReport],
    articles: list[SavedArticle],
    *,
    source_total: int,
) -> str:
    today = date.today()
    date_str = today.isoformat()
    saved = sum(i.saved for i in items)
    failed = sum(i.failed for i in items)
    ok_sources = sum(1 for i in items if not i.error)
    with_saves = [i for i in items if i.saved > 0]

    sorted_articles = sorted(
        articles,
        key=lambda a: (a.published or "", a.source_name, a.title),
        reverse=True,
    )

    lines: list[str] = [
        f"# 📰 AI 公司博客简报 — {date_str}",
        "",
        f"> 扫描 {source_total} 家 AI 公司博客，本次新收录 **{saved}** 篇"
        + (f"（失败 {failed} 篇）" if failed else ""),
        "",
    ]

    if sorted_articles:
        lines.append("## 🏆 本次新文")
        lines.append("")
        for index, article in enumerate(sorted_articles[:10]):
            medal = ["🥇", "🥈", "🥉"][index] if index < 3 else f"{index + 1}."
            when = _humanize_published(article.published)
            lines.append(f"{medal} **{article.title}**")
            lines.append("")
            meta = f"[{article.title}]({article.url}) — {article.source_name}"
            if when:
                meta += f" · {when}"
            lines.append(meta)
            if article.description:
                lines.append("")
                lines.append(f"> {article.description}")
            note_rel = _rel_from_report(article.filepath)
            if note_rel:
                lines.append("")
                lines.append(f"📎 [Obsidian 笔记]({note_rel})")
            lines.append("")
        lines.append("---")
        lines.append("")
    else:
        lines.append("本次各来源均无新保存文章。")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 📊 数据概览")
    lines.append("")
    lines.append("| 扫描源 | 本次保存 | 失败 | 有新文的公司 |")
    lines.append("|:---:|:---:|:---:|:---:|")
    lines.append(
        f"| {ok_sources}/{source_total} | **{saved}** 篇 | {failed} 篇 | "
        f"**{len(with_saves)}** 家 |"
    )
    lines.append("")

    if with_saves:
        lines.append("### 公司分布")
        lines.append("")
        lines.append("```mermaid")
        lines.append("pie showData")
        lines.append('    title "本次新文公司分布"')
        for item in sorted(with_saves, key=lambda x: (-x.saved, x.name)):
            safe = item.name.replace('"', "'")
            lines.append(f'    "{safe}" : {item.saved}')
        lines.append("```")
        lines.append("")
        names = "、".join(i.name for i in sorted(with_saves, key=lambda x: (-x.saved, x.name)))
        lines.append(f"🏷️ {names}")
        lines.append("")

    errors = [i for i in items if i.error]
    if errors:
        lines.append("### 发现失败")
        lines.append("")
        for item in errors:
            lines.append(f"- {item.name}：{item.error}")
        lines.append("")

    lines.append("---")
    lines.append("")

    if sorted_articles:
        groups: dict[str, list[SavedArticle]] = {}
        for article in sorted_articles:
            groups.setdefault(article.source_name, []).append(article)
        index = 0
        for company, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"## {company}")
            lines.append("")
            for article in group:
                index += 1
                when = _humanize_published(article.published)
                lines.append(f"### {index}. {article.title}")
                lines.append("")
                meta = f"[{article.title}]({article.url}) — **{article.source_name}**"
                if when:
                    meta += f" · {when}"
                lines.append(meta)
                if article.description:
                    lines.append("")
                    lines.append(f"> {article.description}")
                note_rel = _rel_from_report(article.filepath)
                if note_rel:
                    lines.append("")
                    lines.append(f"📎 [Obsidian 笔记]({note_rel})")
                lines.append("")
                lines.append("---")
                lines.append("")

    now = datetime.now().strftime("%H:%M")
    lines.append(
        f"*生成于 {date_str} {now} | 扫描 {ok_sources}/{source_total} 源 → 新收录 {saved} 篇*"
    )
    lines.append("")
    return "\n".join(lines)


def excerpt_from_body(body: str, description: str = "", limit: int = 280) -> str:
    if description.strip():
        text = re.sub(r"\s+", " ", description).strip()
        return text[:limit] + ("…" if len(text) > limit else "")
    for para in body.split("\n\n"):
        text = re.sub(r"^#{1,6}\s+", "", para).strip()
        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 40:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def _humanize_published(value: str) -> str:
    if not value:
        return ""
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10]
    if published.tzinfo is not None:
        published = published.astimezone().replace(tzinfo=None)
    diff = datetime.now() - published
    mins = int(diff.total_seconds() // 60)
    if mins < 0:
        return published.date().isoformat()
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    if days < 7:
        return f"{days} 天前"
    return published.date().isoformat()


def _rel_from_report(article_path: Path) -> str:
    return f"./{article_path.parent.name}/{article_path.name}"

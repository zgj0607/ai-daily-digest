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
    problem: str = ""
    method: str = ""
    validation: str = ""
    experiment: str = ""


def write_blog_report(
    path: Path,
    items: list[SourceReport],
    articles: list[SavedArticle],
    *,
    source_total: int,
    highlights: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        generate_blog_report(items, articles, source_total=source_total, highlights=highlights),
        encoding="utf-8",
    )
    return path


def generate_blog_report(
    items: list[SourceReport],
    articles: list[SavedArticle],
    *,
    source_total: int,
    highlights: str = "",
) -> str:
    date_str = date.today().isoformat()
    saved = sum(i.saved for i in items)
    failed = sum(i.failed for i in items)
    ok_sources = sum(1 for i in items if not i.error)
    with_saves = sorted((i for i in items if i.saved > 0), key=lambda x: (-x.saved, x.name))
    sorted_articles = sorted(
        articles, key=lambda a: (a.published or "", a.source_name, a.title), reverse=True
    )

    lines = [
        f"# 📰 AI 公司博客简报 — {date_str}",
        "",
        f"> 扫描 {source_total} 家 AI 公司博客，本次新收录 **{saved}** 篇"
        + (f"（失败 {failed} 篇）" if failed else "")
        + "；按「问题 / 方法 / 验证 / 智能客服小实验」四维提炼",
        "",
    ]
    if highlights.strip():
        lines += ["## 📝 行业迁移启示", "", highlights.strip(), "", "---", ""]

    if sorted_articles:
        lines += ["## 🏆 本次新文", ""]
        for i, article in enumerate(sorted_articles[:10]):
            medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."
            lines += _article_block(article, f"{medal} **{article.title}**", bold_company=False)
        lines += ["---", ""]
    else:
        lines += ["本次各来源均无新保存文章。", "", "---", ""]

    lines += [
        "## 📊 数据概览",
        "",
        "| 扫描源 | 本次保存 | 失败 | 有新文的公司 |",
        "|:---:|:---:|:---:|:---:|",
        f"| {ok_sources}/{source_total} | **{saved}** 篇 | {failed} 篇 | **{len(with_saves)}** 家 |",
        "",
    ]
    if with_saves:
        lines += ["### 公司分布", "", "```mermaid", "pie showData", '    title "本次新文公司分布"']
        lines += [f'    "{i.name.replace(chr(34), chr(39))}" : {i.saved}' for i in with_saves]
        lines += ["```", "", f"🏷️ {'、'.join(i.name for i in with_saves)}", ""]

    errors = [i for i in items if i.error]
    if errors:
        lines += ["### 发现失败", ""] + [f"- {i.name}：{i.error}" for i in errors] + [""]

    lines += ["---", ""]
    if sorted_articles:
        groups: dict[str, list[SavedArticle]] = {}
        for article in sorted_articles:
            groups.setdefault(article.source_name, []).append(article)
        index = 0
        for company, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines += [f"## {company}", ""]
            for article in group:
                index += 1
                lines += _article_block(
                    article, f"### {index}. {article.title}", bold_company=True
                )
                lines += ["---", ""]

    now = datetime.now().strftime("%H:%M")
    lines += [
        f"*生成于 {date_str} {now} | 扫描 {ok_sources}/{source_total} 源 → 新收录 {saved} 篇*",
        "*提炼视角：具体问题 → 方法与理由 → 验证与数据条件 → "
        "智能客服（呼入/外呼/文本）在生活服务·金融·保险·支付的小实验*",
        "",
    ]
    return "\n".join(lines)


def _article_block(article: SavedArticle, heading: str, *, bold_company: bool) -> list[str]:
    when = _humanize_published(article.published)
    company = f"**{article.source_name}**" if bold_company else article.source_name
    meta = f"[{article.title}]({article.url}) — {company}" + (f" · {when}" if when else "")
    lines = [heading, "", meta]
    if article.problem or article.method or article.validation or article.experiment:
        lines.append("")
        for label, value in (
            ("面对的问题", article.problem),
            ("方法与理由", article.method),
            ("验证与条件", article.validation),
            ("客服小实验", article.experiment),
        ):
            if value:
                lines.append(f"- **{label}**：{value}")
    elif article.description:
        lines += ["", f"> {article.description}"]
    lines += ["", f"📎 [Obsidian 笔记](./{article.filepath.parent.name}/{article.filepath.name})", ""]
    return lines


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
    mins = int((datetime.now() - published).total_seconds() // 60)
    if mins < 0:
        return published.date().isoformat()
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    return f"{days} 天前" if days < 7 else published.date().isoformat()

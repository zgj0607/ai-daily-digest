from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from digest.config import load_settings
from digest.crawl import run, show_status
from digest.clippings import DEFAULT_DIR, run_clippings
from digest.titles import run_titles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 AI 公司博客，写入 Obsidian Markdown")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认 sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="按进度爬取；爬取落盘与翻译流水线并行")
    run_p.add_argument("--source", help="只爬一个来源，例如 anthropic")
    run_p.add_argument("--limit", type=int, help="每个来源本次最多新抓多少篇")
    run_p.add_argument("--output-dir", type=Path, help="覆盖 Markdown 输出目录")
    run_p.add_argument("--state", type=Path, help="覆盖进度数据库路径")
    run_p.add_argument("--article-concurrency", type=int, help="爬取并发（默认 3）")
    run_p.add_argument("--source-concurrency", type=int, help="发现并发（默认 3）")
    run_p.add_argument("--llm-concurrency", type=int, help="翻译/LLM 并发（默认 3）")

    status_p = sub.add_parser("status", help="查看每个来源的爬取进度")
    status_p.add_argument("--state", type=Path, help="覆盖进度数据库路径")

    titles_p = sub.add_parser("titles", help="扫描已保存笔记，把英文标题译成中文并改文件名")
    titles_p.add_argument("--output-dir", type=Path, help="覆盖 Markdown 输出目录")
    titles_p.add_argument("--state", type=Path, help="覆盖进度数据库路径")
    titles_p.add_argument("--llm-concurrency", type=int, help="本地模型并发（默认 3）")

    clip_p = sub.add_parser("clippings", help="扫描日报剪藏，把英文标题和未译正文一次性译成中文")
    clip_p.add_argument("--output-dir", type=Path, help=f"剪藏目录，默认 {DEFAULT_DIR}")
    clip_p.add_argument("--llm-concurrency", type=int, help="本地模型并发（默认 3）")

    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if getattr(args, "state", None):
        settings = replace(settings, state_db=args.state)
    if getattr(args, "output_dir", None):
        settings = replace(settings, output_dir=args.output_dir)
    overrides = {
        key: max(1, getattr(args, key))
        for key in ("article_concurrency", "source_concurrency", "llm_concurrency")
        if getattr(args, key, None)
    }
    if overrides:
        settings = replace(settings, **overrides)

    if args.command == "status":
        return show_status(settings)
    if args.command == "titles":
        return run_titles(settings)
    if args.command == "clippings":
        return run_clippings(settings, args.output_dir)
    return run(settings, only=args.source, limit=args.limit)

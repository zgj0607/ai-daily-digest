from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from digest.config import load_settings
from digest.crawl import run, show_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 AI 公司博客，写入 Obsidian Markdown")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认 sources.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="按进度爬取。未完成首次全量的来源会继续抓完")
    run_parser.add_argument("--source", help="只爬一个来源，例如 anthropic")
    run_parser.add_argument("--limit", type=int, help="每个来源本次最多新抓多少篇，用于试跑")
    run_parser.add_argument("--output-dir", type=Path, help="覆盖 Markdown 输出目录")
    run_parser.add_argument("--state", type=Path, help="覆盖进度数据库路径")
    run_parser.add_argument("--article-concurrency", type=int, help="同时处理文章数（默认 3）")
    run_parser.add_argument("--source-concurrency", type=int, help="同时发现来源数（默认 3）")
    run_parser.add_argument("--llm-concurrency", type=int, help="全局 LLM 并发上限（默认 3）")
    run_parser.add_argument(
        "--summary",
        type=Path,
        help="额外复制一份简报到该路径（默认已写入 output_dir/YYYYMMDD-公司博客简报.md）",
    )

    status_parser = subparsers.add_parser("status", help="查看每个来源的爬取进度")
    status_parser.add_argument("--state", type=Path, help="覆盖进度数据库路径")

    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    state_path = getattr(args, "state", None)
    output_dir = getattr(args, "output_dir", None)
    if state_path:
        settings = replace(settings, state_db=state_path)
    if output_dir:
        settings = replace(settings, output_dir=output_dir)
    overrides = {}
    if getattr(args, "article_concurrency", None):
        overrides["article_concurrency"] = max(1, args.article_concurrency)
    if getattr(args, "source_concurrency", None):
        overrides["source_concurrency"] = max(1, args.source_concurrency)
    if getattr(args, "llm_concurrency", None):
        overrides["llm_concurrency"] = max(1, args.llm_concurrency)
    if overrides:
        settings = replace(settings, **overrides)

    if args.command == "status":
        return show_status(settings)
    return run(settings, only=args.source, limit=args.limit, summary_path=args.summary)

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "sources.yaml"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    index_url: str
    include: re.Pattern[str]
    enabled: bool = True
    feed_url: str = ""
    sitemap_url: str = ""
    exclude: tuple[re.Pattern[str], ...] = ()

    def accepts(self, url: str) -> bool:
        if not self.include.search(url):
            return False
        return not any(pattern.search(url) for pattern in self.exclude)


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    assets_dir: Path
    state_db: Path
    request_interval_seconds: float
    user_agent: str
    max_attempts: int
    # 并行：来源发现 / 文章处理 / 全局 LLM 请求上限（对齐本地 4090 画像）
    source_concurrency: int
    article_concurrency: int
    llm_concurrency: int
    sources: tuple[Source, ...]

    def enabled_sources(self, only: str | None = None) -> list[Source]:
        sources = [source for source in self.sources if source.enabled]
        if only:
            sources = [source for source in sources if source.id == only]
            if not sources:
                known = ", ".join(source.id for source in self.sources)
                raise SystemExit(f"未知来源 {only}。可选: {known}")
        return sources


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or DEFAULT_CONFIG
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = config_path.parent

    sources: list[Source] = []
    for item in raw.get("sources") or []:
        if not item.get("sitemap_url") and not item.get("feed_url"):
            raise SystemExit(f"来源 {item.get('id')} 需要 sitemap_url 或 feed_url")
        include = item.get("include")
        if not include:
            raise SystemExit(f"来源 {item.get('id')} 需要 include")
        sources.append(
            Source(
                id=str(item["id"]),
                name=str(item["name"]),
                index_url=str(item.get("index_url") or ""),
                include=re.compile(include),
                enabled=bool(item.get("enabled", True)),
                feed_url=str(item.get("feed_url") or ""),
                sitemap_url=str(item.get("sitemap_url") or ""),
                exclude=tuple(re.compile(pattern) for pattern in item.get("exclude") or []),
            )
        )

    output_dir = Path(raw.get("output_dir") or "output")
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    # 默认与日报 Obsidian 库一致：raw/ai-company-blogs → raw/assets
    assets_dir = Path(raw.get("assets_dir") or (output_dir.parent / "assets"))
    if not assets_dir.is_absolute():
        assets_dir = root / assets_dir
    state_db = Path(raw.get("state_db") or "state/crawl.sqlite")
    if not state_db.is_absolute():
        state_db = root / state_db

    return Settings(
        output_dir=output_dir,
        assets_dir=assets_dir,
        state_db=state_db,
        request_interval_seconds=float(raw.get("request_interval_seconds") or 0.3),
        user_agent=str(raw.get("user_agent") or "ai-company-blog-digest/0.1"),
        max_attempts=int(raw.get("max_attempts") or 3),
        source_concurrency=max(1, int(raw.get("source_concurrency") or 3)),
        article_concurrency=max(1, int(raw.get("article_concurrency") or 3)),
        llm_concurrency=max(1, int(raw.get("llm_concurrency") or 3)),
        sources=tuple(sources),
    )

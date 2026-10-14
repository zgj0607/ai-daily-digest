from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "sources.yaml"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
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
    state_db: Path
    request_interval_seconds: float
    user_agent: str
    max_attempts: int
    source_concurrency: int
    article_concurrency: int
    llm_concurrency: int
    sources: tuple[Source, ...]

    def enabled_sources(self, only: str | None = None) -> list[Source]:
        sources = [s for s in self.sources if s.enabled]
        if only:
            sources = [s for s in sources if s.id == only]
            if not sources:
                known = ", ".join(s.id for s in self.sources)
                raise SystemExit(f"未知来源 {only}。可选: {known}")
        return sources


def load_settings(path: Path | None = None) -> Settings:
    import yaml

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
                include=re.compile(include),
                enabled=bool(item.get("enabled", True)),
                feed_url=str(item.get("feed_url") or ""),
                sitemap_url=str(item.get("sitemap_url") or ""),
                exclude=tuple(re.compile(p) for p in item.get("exclude") or []),
            )
        )

    def resolve(value: object, default: str) -> Path:
        path_value = Path(str(value or default))
        return path_value if path_value.is_absolute() else root / path_value

    return Settings(
        output_dir=resolve(raw.get("output_dir"), "output"),
        state_db=resolve(raw.get("state_db"), "state/crawl.sqlite"),
        request_interval_seconds=float(raw.get("request_interval_seconds") or 0.3),
        user_agent=str(raw.get("user_agent") or "ai-company-blog-digest/0.1"),
        max_attempts=int(raw.get("max_attempts") or 3),
        source_concurrency=max(1, int(raw.get("source_concurrency") or 3)),
        article_concurrency=max(1, int(raw.get("article_concurrency") or 3)),
        llm_concurrency=max(1, int(raw.get("llm_concurrency") or 3)),
        sources=tuple(sources),
    )

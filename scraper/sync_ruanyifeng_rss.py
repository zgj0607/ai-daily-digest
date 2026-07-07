#!/usr/bin/env python3
"""
阮一峰周刊 RSS 增量同步脚本

流程：
1. 抓取最新周刊列表，与 weekly_urls.json 对比，仅处理新增周刊
2. 从新增周刊提取外部博客链接，追加到 ruanyifeng_blogs.csv
3. 仅对 CSV 中尚未探测 RSS 的博客地址做 RSS 探测
4. 合并生成项目根目录 rss.txt；每次增量更新时先校验已有地址，再校验新增地址
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRAPER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRAPER_DIR.parent
WEEKLY_URLS_FILE = SCRAPER_DIR / "weekly_urls.json"
CSV_FILE = SCRAPER_DIR / "ruanyifeng_blogs.csv"
RSS_TXT_FILE = PROJECT_ROOT / "rss.txt"
LOG_FILE = SCRAPER_DIR / "sync.log"

BASE_URL = "https://www.ruanyifeng.com/blog/weekly/"
MAX_WORKERS_WEEKLY = 2
MAX_WORKERS_RSS = 10
MAX_WORKERS_VALIDATE = 15
RSS_VALIDATE_TIMEOUT = 10

CSV_HEADERS = ["周刊名称", "周刊链接", "博客名称", "博客链接", "RSS订阅地址"]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXCLUDE_DOMAINS = {
    "github.com", "youtube.com", "weibo.com", "zhihu.com",
    "juejin.cn", "segmentfault.com", "csdn.net", "blog.csdn.net",
    "aliyun.com", "tencent.com", "baidu.com", "douban.com",
    "wikipedia.org", "w3.org", "ietf.org", "stackoverflow.com",
    "medium.com", "dev.to", "npmjs.com", "pypi.org", "docker.com",
    "microsoft.com", "google.com", "apple.com", "amazon.com",
    "163.com", "sina.com.cn", "qq.com", "sohu.com", "ifeng.com",
    "36kr.com", "pingwest.com", "techcrunch.com", "wired.com",
    "theverge.com", "arstechnica.com", "engadget.com",
    "reddit.com", "hackernews.com", "news.ycombinator.com",
    "nature.com", "sciencemag.org", "ieee.org", "acm.org",
    "arxiv.org", "slideshare.net", "speakerdeck.com",
    "loom.com", "vimeo.com", "dailymotion.com",
    "cloudflare.com", "vercel.com", "netlify.com", "heroku.com",
    "digitalocean.com", "aws.amazon.com", "azure.microsoft.com",
    "notion.so", "airtable.com", "figma.com", "sketch.com",
    "canva.com", "unsplash.com", "pexels.com",
    "chrome.google.com", "addons.mozilla.org", "archive.org",
    "cnbc.com", "bloomberg.com", "reuters.com",
    "wsj.com", "ft.com", "nytimes.com", "bbc.com", "theguardian.com",
    "twitter.com", "linkedin.com", "facebook.com", "instagram.com",
    "xcancel.com", "archive.ph", "creativecommons.org",
    "openai.com", "labex.io",
}

COMMON_RSS_PATHS = [
    "/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/blog/feed", "/index.xml",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

counter_lock = threading.Lock()
processed_weekly_count = 0
found_blog_count = 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    LOG_FILE.write_text("", encoding="utf-8")
    logger = logging.getLogger("sync_ruanyifeng_rss")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


logger = setup_logging()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        url = urljoin("https://www.ruanyifeng.com", url)
    return url.rstrip("/")


def is_personal_blog(url: str, domain: str) -> bool:
    domain = domain.lower()
    if any(exclude in domain for exclude in EXCLUDE_DOMAINS):
        return False
    if domain.endswith(".gov") or domain.endswith(".edu"):
        return False
    if "company" in domain or "inc." in domain:
        return False
    parts = domain.split(".")
    if len(parts) <= 2:
        return True
    personal_suffixes = [
        ".io", ".me", ".dev", ".tech", ".app", ".site",
        ".space", ".blog", ".cc", ".online",
    ]
    if any(domain.endswith(suffix) for suffix in personal_suffixes):
        return True
    return "notion.site" in domain


def fetch_page(url: str, timeout: int = 15, max_retries: int = 3) -> requests.Response | None:
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp
            if resp.status_code == 404:
                return None
        except requests.RequestException:
            if attempt < max_retries - 1:
                time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# Weekly list
# ---------------------------------------------------------------------------


def fetch_weekly_list() -> list[dict]:
    logger.info("抓取阮一峰周刊列表...")
    resp = fetch_page(BASE_URL)
    if resp is None:
        raise RuntimeError(f"无法获取周刊列表页: {BASE_URL}")

    soup = BeautifulSoup(resp.text, "html.parser")
    weekly_list: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = normalize_url(anchor["href"])
        if "weekly-issue-" not in href and "weely-issue-" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        weekly_list.append({
            "title": anchor.get_text(strip=True),
            "url": href,
        })

    return [
        {"issue": index, "title": item["title"], "url": item["url"]}
        for index, item in enumerate(weekly_list, start=1)
    ]


def load_weekly_urls() -> list[dict]:
    if not WEEKLY_URLS_FILE.exists():
        return []
    with WEEKLY_URLS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    for index, item in enumerate(data, start=1):
        item["issue"] = index
        item["url"] = normalize_url(item.get("url", ""))
        item["title"] = item.get("title", "")
    return data


def save_weekly_urls(weekly_list: list[dict]) -> None:
    with WEEKLY_URLS_FILE.open("w", encoding="utf-8") as f:
        json.dump(weekly_list, f, ensure_ascii=False, indent=2)


def find_new_weeklies(fetched: list[dict], existing: list[dict]) -> list[dict]:
    processed_urls = {normalize_url(item["url"]) for item in existing}
    return [item for item in fetched if normalize_url(item["url"]) not in processed_urls]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def load_csv_rows() -> list[dict]:
    if not CSV_FILE.exists():
        return []

    rows: list[dict] = []
    with CSV_FILE.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "weekly_name": row.get("周刊名称", "").strip(),
                "weekly_url": normalize_url(row.get("周刊链接", "")),
                "blog_name": row.get("博客名称", "").strip(),
                "blog_url": normalize_url(row.get("博客链接", "")),
                "rss": row.get("RSS订阅地址", "").strip(),
            })
    return rows


def save_csv_rows(rows: list[dict]) -> None:
    with CSV_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for row in rows:
            writer.writerow([
                row.get("weekly_name", ""),
                row.get("weekly_url", ""),
                row.get("blog_name", ""),
                row.get("blog_url", ""),
                row.get("rss", ""),
            ])


def build_known_blog_index(rows: list[dict]) -> dict[str, str]:
    """blog_url -> rss（空字符串表示已探测但未找到）"""
    index: dict[str, str] = {}
    for row in rows:
        blog_url = row.get("blog_url", "")
        if not blog_url:
            continue
        rss = row.get("rss", "")
        if blog_url not in index or (rss and not index[blog_url]):
            index[blog_url] = rss
    return index


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_blogs_from_weekly(
    weekly: dict,
    known_blog_urls: set[str],
    session_seen: set[str],
) -> list[dict]:
    global processed_weekly_count, found_blog_count

    url = weekly["url"]
    issue = weekly["issue"]
    title = weekly["title"]
    blogs: list[dict] = []

    logger.info("[%s] 请求: %s...", issue, title[:30])
    resp = fetch_page(url)
    if resp is None:
        logger.warning("[%s] ✗ 页面获取失败", issue)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    for link in soup.find_all("a", href=True):
        href = normalize_url(link.get("href", ""))
        if not href.startswith("http"):
            continue
        if href.startswith("https://www.ruanyifeng.com") or "ruanyifeng.com" in href:
            continue
        if href in session_seen or href in known_blog_urls:
            continue

        try:
            domain = urlparse(href).netloc.lower()
            if not is_personal_blog(href, domain):
                continue
            session_seen.add(href)
            text = link.get_text(strip=True)
            blogs.append({
                "weekly_name": title,
                "weekly_url": url,
                "blog_name": text[:100] if text else domain,
                "blog_url": href,
                "rss": "",
            })
        except Exception:
            continue

    with counter_lock:
        processed_weekly_count += 1
        found_blog_count += len(blogs)

    logger.info("[%s] ✓ 找到 %s 个新博客 (累计 %s)", issue, len(blogs), found_blog_count)
    return blogs


def check_rss(blog_url: str) -> str:
    parsed = urlparse(blog_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    for path in COMMON_RSS_PATHS:
        rss_url = base_url + path
        try:
            resp = requests.get(rss_url, headers=HEADERS, timeout=3, allow_redirects=True)
            if resp.status_code != 200:
                continue
            content = resp.text[:500].lower()
            if "<?xml" in content or "<rss" in content or "<feed" in content:
                return rss_url
        except requests.RequestException:
            continue
    return ""


def probe_rss_for_blogs(blogs: list[dict], known_blog_index: dict[str, str]) -> int:
    to_probe = [
        blog for blog in blogs
        if blog["blog_url"] not in known_blog_index
    ]
    cached = len(blogs) - len(to_probe)
    if cached:
        logger.info("跳过 %s 个已探测 RSS 的博客", cached)

    if not to_probe:
        return 0

    logger.info("=" * 60)
    logger.info("探测 RSS，共 %s 个新博客 (并发 %s)", len(to_probe), MAX_WORKERS_RSS)
    logger.info("=" * 60)

    rss_found = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_RSS) as executor:
        future_map = {
            executor.submit(check_rss, blog["blog_url"]): blog
            for blog in to_probe
        }
        for future in as_completed(future_map):
            blog = future_map[future]
            completed += 1
            try:
                rss = future.result()
            except Exception as exc:
                logger.warning("RSS 探测失败 %s: %s", blog["blog_url"], exc)
                rss = ""

            blog["rss"] = rss
            known_blog_index[blog["blog_url"]] = rss
            if rss:
                rss_found += 1
                logger.info(
                    "  [%s/%s] ✓ %s -> %s",
                    completed, len(to_probe), blog["blog_name"][:25], rss[:60],
                )
            elif completed % 20 == 0:
                logger.info("  [%s/%s] 进度: 已找到 %s 个 RSS", completed, len(to_probe), rss_found)

    logger.info("RSS 探测完成: %s/%s", rss_found, len(to_probe))
    return rss_found


# ---------------------------------------------------------------------------
# rss.txt
# ---------------------------------------------------------------------------


def load_existing_rss_txt() -> list[str]:
    if not RSS_TXT_FILE.exists():
        return []
    lines: list[str] = []
    with RSS_TXT_FILE.open(encoding="utf-8") as f:
        for line in f:
            value = line.strip()
            if value and not value.startswith("#"):
                lines.append(value)
    return lines


def write_rss_txt(urls: list[str]) -> None:
    with RSS_TXT_FILE.open("w", encoding="utf-8") as f:
        for url in urls:
            f.write(f"{url}\n")


def is_valid_rss_feed(url: str) -> bool:
    """检测 URL 是否为可访问的有效 RSS/Atom 订阅源。"""
    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=RSS_VALIDATE_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return False

        sample = resp.text[:4000].lower()
        if not sample.strip():
            return False

        has_feed_marker = (
            "<rss" in sample
            or "<feed" in sample
            or "<rdf:" in sample
        )
        if not has_feed_marker:
            return False

        # 排除误探测到的 HTML 页面（含 feed 字样但不是 XML）
        if "<html" in sample and "<?xml" not in sample and "<rss" not in sample and "<feed" not in sample:
            return False

        looks_like_xml = (
            "<?xml" in sample
            or sample.lstrip().startswith("<rss")
            or sample.lstrip().startswith("<feed")
            or sample.lstrip().startswith("<rdf:")
        )
        return looks_like_xml
    except requests.RequestException:
        return False


def validate_rss_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    """并行校验 rss.txt 中的地址，返回 (有效列表, 无效列表)。"""
    if not urls:
        return [], []

    results: dict[str, bool] = {}
    total = len(urls)
    completed = 0

    logger.info("校验 RSS 地址，共 %s 个 (并发 %s)", total, MAX_WORKERS_VALIDATE)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_VALIDATE) as executor:
        future_map = {
            executor.submit(is_valid_rss_feed, url): url
            for url in urls
        }
        for future in as_completed(future_map):
            url = future_map[future]
            completed += 1
            try:
                results[url] = future.result()
            except Exception as exc:
                logger.warning("校验异常 %s: %s", url, exc)
                results[url] = False

            if completed % 50 == 0 or completed == total:
                valid_so_far = sum(1 for ok in results.values() if ok)
                logger.info("  校验进度: %s/%s (有效 %s)", completed, total, valid_so_far)

    valid = [url for url in urls if results.get(url)]
    invalid = [url for url in urls if not results.get(url)]
    return valid, invalid


def purge_invalid_rss_from_csv(rows: list[dict], invalid_rss: set[str]) -> int:
    """清除 CSV 中指向无效 RSS 的订阅地址。"""
    cleared = 0
    for row in rows:
        if row.get("rss", "") in invalid_rss:
            row["rss"] = ""
            cleared += 1
    return cleared


def validate_and_prune_existing_rss_txt(csv_rows: list[dict]) -> tuple[list[str], int]:
    """校验 rss.txt 中已有地址，无效的直接移除，并同步清理 CSV。"""
    existing = load_existing_rss_txt()
    if not existing:
        logger.info("rss.txt 为空，跳过已有地址校验")
        return [], 0

    logger.info("校验 rss.txt 已有地址: %s 条", len(existing))
    valid_urls, invalid_urls = validate_rss_urls(existing)

    write_rss_txt(valid_urls)

    if invalid_urls:
        logger.warning("从 rss.txt 移除无效地址 %s 条:", len(invalid_urls))
        for url in invalid_urls[:10]:
            logger.warning("  ✗ %s", url)
        if len(invalid_urls) > 10:
            logger.warning("  ... 还有 %s 条", len(invalid_urls) - 10)

        cleared = purge_invalid_rss_from_csv(csv_rows, set(invalid_urls))
        if cleared:
            logger.info("已从 CSV 清除 %s 条无效 RSS 记录", cleared)
    else:
        logger.info("rss.txt 已有地址全部有效")

    logger.info("rss.txt 保留有效地址: %s 条", len(valid_urls))
    return valid_urls, len(invalid_urls)


def merge_and_validate_new_rss_urls(
    existing_urls: list[str],
    csv_rows: list[dict],
) -> tuple[list[str], int]:
    """合并 CSV 中的新 RSS，仅校验新增地址后写入 rss.txt。"""
    seen = set(existing_urls)
    merged = list(existing_urls)
    new_urls: list[str] = []

    for row in csv_rows:
        rss = row.get("rss", "").strip()
        if rss and rss not in seen:
            seen.add(rss)
            new_urls.append(rss)

    if not new_urls:
        logger.info("没有新增 RSS 地址需要合并")
        return merged, 0

    logger.info("校验新增 RSS 地址: %s 条", len(new_urls))
    valid_new, invalid_new = validate_rss_urls(new_urls)

    if invalid_new:
        logger.warning("新增地址中无效 %s 条:", len(invalid_new))
        for url in invalid_new[:10]:
            logger.warning("  ✗ %s", url)
        if len(invalid_new) > 10:
            logger.warning("  ... 还有 %s 条", len(invalid_new) - 10)
        purge_invalid_rss_from_csv(csv_rows, set(invalid_new))

    merged.extend(valid_new)
    write_rss_txt(merged)

    logger.info(
        "已写入 %s: 总计 %s 条 (新增有效 %s 条, 新增无效 %s 条)",
        RSS_TXT_FILE, len(merged), len(valid_new), len(invalid_new),
    )
    return merged, len(invalid_new)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global processed_weekly_count, found_blog_count

    start_time = time.time()
    logger.info("=" * 60)
    logger.info("阮一峰周刊 RSS 增量同步")
    logger.info("开始: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    existing_weeklies = load_weekly_urls()
    existing_rows = load_csv_rows()
    known_blog_index = build_known_blog_index(existing_rows)
    known_blog_urls = set(known_blog_index.keys())

    logger.info("已有周刊记录: %s", len(existing_weeklies))
    logger.info("已有博客记录: %s", len(existing_rows))
    logger.info("已有 RSS 记录: %s", sum(1 for rss in known_blog_index.values() if rss))

    logger.info("=" * 60)
    logger.info("【阶段 0】校验 rss.txt 已有地址")
    logger.info("=" * 60)
    valid_existing_rss, removed_existing_rss = validate_and_prune_existing_rss_txt(existing_rows)
    if removed_existing_rss:
        save_csv_rows(existing_rows)

    fetched_weeklies = fetch_weekly_list()
    logger.info("网站周刊总数: %s", len(fetched_weeklies))

    new_weeklies = find_new_weeklies(fetched_weeklies, existing_weeklies)
    logger.info("新增周刊: %s", len(new_weeklies))
    if new_weeklies:
        for item in new_weeklies[:5]:
            logger.info("  + %s", item["title"])
        if len(new_weeklies) > 5:
            logger.info("  ... 还有 %s 期", len(new_weeklies) - 5)
    else:
        logger.info("没有新增周刊，跳过博客提取")

    new_blogs: list[dict] = []
    session_seen: set[str] = set()

    if new_weeklies:
        logger.info("=" * 60)
        logger.info("【阶段 1】从新增周刊提取博客链接 (并发 %s)", MAX_WORKERS_WEEKLY)
        logger.info("=" * 60)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS_WEEKLY) as executor:
            futures = [
                executor.submit(
                    extract_blogs_from_weekly,
                    weekly,
                    known_blog_urls,
                    session_seen,
                )
                for weekly in new_weeklies
            ]
            for future in as_completed(futures):
                try:
                    blogs = future.result()
                    new_blogs.extend(blogs)
                    for blog in blogs:
                        known_blog_urls.add(blog["blog_url"])
                except Exception as exc:
                    logger.error("周刊处理出错: %s", exc)

    logger.info("新增博客链接: %s", len(new_blogs))

    if new_blogs:
        logger.info("=" * 60)
        logger.info("【阶段 2】探测 RSS")
        logger.info("=" * 60)
        probe_rss_for_blogs(new_blogs, known_blog_index)
        existing_rows.extend(new_blogs)

    save_weekly_urls(fetched_weeklies)
    logger.info("已更新 %s (%s 期)", WEEKLY_URLS_FILE, len(fetched_weeklies))

    save_csv_rows(existing_rows)
    logger.info("已更新 %s (%s 条)", CSV_FILE, len(existing_rows))

    logger.info("=" * 60)
    logger.info("【阶段 3】合并新增 RSS 并校验")
    logger.info("=" * 60)
    total_rss, removed_new_rss = merge_and_validate_new_rss_urls(valid_existing_rss, existing_rows)
    removed_rss = removed_existing_rss + removed_new_rss

    if removed_new_rss:
        save_csv_rows(existing_rows)
        logger.info("CSV 已同步清除无效 RSS")

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("同步完成")
    logger.info("耗时: %.1f 秒", elapsed)
    logger.info("处理新增周刊: %s", len(new_weeklies))
    logger.info("新增博客: %s", len(new_blogs))
    logger.info("CSV 总记录: %s", len(existing_rows))
    logger.info("rss.txt 有效 RSS: %s (移除无效 %s)", total_rss, removed_rss)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

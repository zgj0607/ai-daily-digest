#!/usr/bin/env python3
"""
阮一峰周刊个人博客提取工具 v7
- 直接使用从网页提取的真实周刊URL
- 确保不重复提取
- 详细日志和进度展示
"""

import requests
from bs4 import BeautifulSoup
import csv
import time
import logging
import json
from urllib.parse import urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 配置
LOG_FILE = '/Users/zhou/.openclaw/workspace/extract.log'
CSV_FILE = '/Users/zhou/.openclaw/workspace/ruanyifeng_blogs.csv'
WEEKLY_URLS_FILE = '/Users/zhou/.openclaw/workspace/weekly_urls.json'
MAX_WORKERS = 2  # 周刊并发数
MAX_WORKERS_RSS = 10  # RSS并发数

# 清空日志
with open(LOG_FILE, 'w') as f:
    f.write('')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 线程锁
counter_lock = threading.Lock()
processed_count = 0
found_count = 0
checked_blog_urls = set()  # 已检查的博客URL

# 排除的域名
EXCLUDE_DOMAINS = {
    'github.com', 'youtube.com', 'weibo.com', 'zhihu.com',
    'juejin.cn', 'segmentfault.com', 'csdn.net', 'blog.csdn.net',
    'aliyun.com', 'tencent.com', 'baidu.com', 'douban.com',
    'wikipedia.org', 'w3.org', 'ietf.org', 'stackoverflow.com',
    'medium.com', 'dev.to', 'npmjs.com', 'pypi.org', 'docker.com',
    'microsoft.com', 'google.com', 'apple.com', 'amazon.com',
    '163.com', 'sina.com.cn', 'qq.com', 'sohu.com', 'ifeng.com',
    '36kr.com', 'pingwest.com', 'techcrunch.com', 'wired.com',
    'theverge.com', 'arstechnica.com', 'engadget.com',
    'reddit.com', 'hackernews.com', 'news.ycombinator.com',
    'nature.com', 'sciencemag.org', 'ieee.org', 'acm.org',
    'arxiv.org', 'slideshare.net', 'speakerdeck.com',
    'loom.com', 'vimeo.com', 'dailymotion.com',
    'cloudflare.com', 'vercel.com', 'netlify.com', 'heroku.com',
    'digitalocean.com', 'aws.amazon.com', 'azure.microsoft.com',
    'notion.so', 'airtable.com', 'figma.com', 'sketch.com',
    'canva.com', 'unsplash.com', 'pexels.com',
    'chrome.google.com', 'addons.mozilla.org', 'archive.org',
    'cnbc.com', 'bloomberg.com', 'reuters.com',
    'wsj.com', 'ft.com', 'nytimes.com', 'bbc.com', 'theguardian.com',
    'twitter.com', 'linkedin.com', 'facebook.com', 'instagram.com',
    'youtube.com', 'xcancel.com', 'archive.ph', 'creativecommons.org',
    'openai.com', 'labex.io'
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# RSS缓存
RSS_CACHE = {}

def is_personal_blog(url, domain):
    if any(exclude in domain.lower() for exclude in EXCLUDE_DOMAINS):
        return False
    if domain.endswith('.gov') or domain.endswith('.edu'):
        return False
    if 'company' in domain or 'inc.' in domain:
        return False
    parts = domain.split('.')
    if len(parts) <= 2:
        return True
    personal_suffixes = ['.io', '.me', '.dev', '.tech', '.app', '.site', '.space', '.blog', '.cc', '.online']
    if any(domain.endswith(s) for s in personal_suffixes):
        return True
    if 'notion.site' in domain:
        return True
    return False

def fetch_page(url, timeout=15, max_retries=3):
    """带重试的请求"""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                resp.encoding = 'utf-8'
                return resp
            elif resp.status_code == 404:
                return None
        except:
            if attempt < max_retries - 1:
                time.sleep(1)
    return None

def extract_blogs_from_weekly(weekly):
    """从单个周刊提取博客"""
    global processed_count, found_count
    
    url = weekly['url']
    issue = weekly['issue']
    title = weekly['title']
    
    blogs = []
    
    try:
        logger.info(f"[{issue}] 请求: {title[:30]}...")
        resp = fetch_page(url)
        
        if resp is None:
            logger.warning(f"[{issue}] ✗ 页面获取失败")
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            
            if href.startswith('/') or 'ruanyifeng.com' in href:
                continue
            if not href.startswith('http'):
                continue
            
            # 检查是否已处理过
            if href in checked_blog_urls:
                continue
            checked_blog_urls.add(href)
            
            try:
                parsed = urlparse(href)
                domain = parsed.netloc.lower()
                
                if is_personal_blog(href, domain):
                    text = link.get_text(strip=True)
                    blogs.append({
                        'blog_name': text[:100] if text else domain,
                        'blog_url': href,
                        'weekly_name': title,
                        'weekly_url': url,
                        'rss': ''
                    })
            except:
                continue
        
        with counter_lock:
            processed_count += 1
            found_count += len(blogs)
        
        logger.info(f"[{issue}] ✓ 找到 {len(blogs)} 个博客 (累计 {found_count})")
        
    except Exception as e:
        logger.warning(f"[{issue}] 错误: {str(e)[:50]}")
    
    return blogs

def check_rss(blog):
    """检查单个博客RSS"""
    blog_url = blog['blog_url']
    
    if blog_url in RSS_CACHE:
        return blog_url, RSS_CACHE[blog_url]
    
    common_rss_paths = ['/feed', '/rss', '/rss.xml', '/atom.xml', '/feed.xml', '/blog/feed', '/index.xml']
    
    try:
        parsed = urlparse(blog_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        for path in common_rss_paths:
            try:
                rss_url = base_url + path
                resp = requests.get(rss_url, timeout=3, allow_redirects=True)
                if resp.status_code == 200:
                    content = resp.text[:500].lower()
                    if '<?xml' in content or '<rss' in content or '<feed' in content:
                        RSS_CACHE[blog_url] = rss_url
                        return blog_url, rss_url
            except:
                pass
    except:
        pass
    
    RSS_CACHE[blog_url] = ''
    return blog_url, ''

def process_rss_parallel(blogs):
    """并行获取RSS"""
    global rss_count
    rss_count = 0
    total = len(blogs)
    
    logger.info(f"=" * 60)
    logger.info(f"【第二阶段】并行获取RSS，共 {total} 个博客")
    logger.info(f"并发数: {MAX_WORKERS_RSS}")
    logger.info("=" * 60)
    
    rss_found_list = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_RSS) as executor:
        futures = {executor.submit(check_rss, blog): blog for blog in blogs}
        
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                blog_url, rss = future.result()
                if rss:
                    rss_count += 1
                    rss_found_list.append((blogs[completed-1]['blog_name'][:20], rss))
                    for b in blogs:
                        if b['blog_url'] == blog_url:
                            b['rss'] = rss
                            break
                    logger.info(f"  [{completed}/{total}] ✓ RSS: {blogs[completed-1]['blog_name'][:25]} -> {rss[:50]}")
                elif completed % 20 == 0:
                    logger.info(f"  [{completed}/{total}] 进度: {rss_count}个已找到")
            except Exception as e:
                pass
    
    logger.info("=" * 60)
    logger.info(f"RSS完成: {rss_count}/{total} 个有RSS")
    if rss_found_list:
        logger.info("前5个RSS示例:")
        for name, rss in rss_found_list[:5]:
            logger.info(f"  - {name}: {rss}")
    
    return rss_count

def save_csv(blogs, mode='w'):
    with open(CSV_FILE, mode, newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        if mode == 'w':
            writer.writerow(['周刊名称', '周刊链接', '博客名称', '博客链接', 'RSS订阅地址'])
        for blog in blogs:
            writer.writerow([
                blog.get('weekly_name', ''),
                blog.get('weekly_url', ''),
                blog.get('blog_name', ''),
                blog.get('blog_url', ''),
                blog.get('rss', '')
            ])

def load_weekly_urls():
    """从文件加载周刊URL，缺失 issue 时按顺序补全"""
    try:
        with open(WEEKLY_URLS_FILE, 'r', encoding='utf-8') as f:
            urls = json.load(f)
        for i, w in enumerate(urls, 1):
            w.setdefault('issue', i)
        return urls
    except Exception:
        return None

def save_weekly_urls(urls):
    """保存周刊URL到文件"""
    with open(WEEKLY_URLS_FILE, 'w', encoding='utf-8') as f:
        json.dump(urls, f, ensure_ascii=False, indent=2)

def main():
    global processed_count, found_count
    
    logger.info("=" * 60)
    logger.info("阮一峰周刊个人博客提取工具 v7")
    logger.info(f"开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    # 尝试加载保存的周刊URL
    weekly_links = load_weekly_urls()
    
    if weekly_links is None:
        logger.error("未找到周刊URL文件，请先运行提取周刊URL的脚本")
        return
    
    total = len(weekly_links)
    logger.info(f"共 {total} 个周刊需要处理")
    logger.info("=" * 60)
    
    all_blogs = []
    start_time = time.time()
    
    # 第一阶段：提取博客
    logger.info("【第一阶段】从周刊提取博客链接")
    logger.info(f"并发数: {MAX_WORKERS}")
    logger.info("-" * 40)
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_weekly = {executor.submit(extract_blogs_from_weekly, w): w for w in weekly_links}
        
        for future in as_completed(future_to_weekly):
            try:
                blogs = future.result()
                all_blogs.extend(blogs)
                
                # 进度显示
                with counter_lock:
                    if processed_count % 20 == 0:
                        elapsed = time.time() - start_time
                        speed = processed_count / elapsed if elapsed > 0 else 0
                        pct = processed_count * 100 // total
                        logger.info(f"  >> 进度: {processed_count}/{total} ({pct}%) 速度:{speed:.1f}期/秒 累计博客:{found_count}")
                        
                        # 定期保存
                        if processed_count % 50 == 0:
                            unique = []
                            seen = set()
                            for b in all_blogs:
                                if b['blog_url'] not in seen:
                                    seen.add(b['blog_url'])
                                    unique.append(b)
                            save_csv(unique)
                            logger.info(f"  >> 已保存中间结果: {len(unique)} 条")
                
            except Exception as e:
                logger.error(f"处理出错: {e}")
    
    extract_time = time.time() - start_time
    logger.info("-" * 40)
    logger.info(f"第一阶段完成! 耗时: {extract_time:.1f}秒")
    logger.info(f"找到博客(去重前): {len(all_books := all_blogs)}")
    
    # 去重（基于博客URL）
    logger.info("正在去重...")
    unique_blogs = []
    seen_urls = set()
    for b in all_blogs:
        if b['blog_url'] not in seen_urls:
            seen_urls.add(b['blog_url'])
            unique_blogs.append(b)
    
    logger.info(f"去重后: {len(unique_blogs)} 个唯一博客")
    save_csv(unique_blogs)
    
    # 第二阶段：获取RSS
    rss_total = 0
    if unique_blogs:
        rss_total = process_rss_parallel(unique_blogs)
        save_csv(unique_blogs)
    
    total_time = time.time() - start_time
    
    logger.info("=" * 60)
    logger.info("提取完成!")
    logger.info(f"总耗时: {total_time:.1f} 秒")
    logger.info(f"处理周刊: {processed_count}/{total}")
    logger.info(f"找到博客: {len(unique_blogs)}")
    logger.info(f"有RSS订阅: {rss_total}")
    logger.info(f"输出文件: {CSV_FILE}")
    logger.info("=" * 60)

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
抓取阮一峰周刊列表：名称和地址保存到 JSON
"""

import json
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://www.ruanyifeng.com/blog/weekly/"
OUTPUT_JSON = "./weekly_urls.json"

def get_weekly_list():
    """请求周刊列表页，解析 HTML，直接提取周刊名称和地址"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    resp = requests.get(BASE_URL, headers=headers)
    resp.raise_for_status()
    text = resp.content.decode('utf-8', errors='replace')
    soup = BeautifulSoup(text, 'html.parser')

    weekly_list = []
    seen = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if 'weekly-issue-' not in href and 'weely-issue-' not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(strip=True)
        weekly_list.append({'title': title, 'url': href})

    return [{'issue': i, 'title': w['title'], 'url': w['url']} for i, w in enumerate(weekly_list, start=1)]

def main():
    print("获取周刊列表...")
    weekly_list = get_weekly_list()
    print(f"找到 {len(weekly_list)} 期周刊")

    for w in weekly_list[:5]:
        print(f"  - {w['title']}: {w['url']}")

    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(weekly_list, f, ensure_ascii=False, indent=2)

    print(f"\n周刊列表已保存到 {OUTPUT_JSON}")

if __name__ == '__main__':
    main()

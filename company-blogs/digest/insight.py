"""按业务视角提炼公司博客：问题 / 方法 / 验证 / 智能客服小实验。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from digest.content import read_markdown_body
from digest.report import SavedArticle
from digest.translate import Translator, parse_llm_json

_BODY_LIMIT = 7000
_INDUSTRY_LENS = (
    "基于智能体的智能客服（呼入、外呼、文本），面向生活服务"
    "（家修、家政、美容、牙医诊所、体检预约、快递、大件物流、无人配送、网约车）、"
    "金融、保险、支付等行业客户"
)


def enrich_insights(
    articles: list[SavedArticle],
    translator: Translator,
    *,
    concurrency: int = 3,
) -> str:
    if not articles:
        return ""

    print(f"[insight] 提炼 {len(articles)} 篇（同时最多 {concurrency} 路，按模型分摊）…")
    workers = max(1, min(concurrency, len(articles)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_analyze_one, a, translator): a for a in articles}
        for future in as_completed(futures):
            article = futures[future]
            try:
                future.result()
                print(f"[insight] 完成 {article.source_name}: {article.title}")
            except Exception as exc:
                print(f"[insight] 失败 {article.source_name}: {article.title}: {exc}")

    try:
        text = _synthesize_highlights(articles, translator)
        print("[insight] 行业迁移启示已生成")
        return text
    except Exception as exc:
        print(f"[insight] 行业迁移启示生成失败: {exc}")
        return ""


def _analyze_one(article: SavedArticle, translator: Translator) -> None:
    try:
        body = read_markdown_body(article.filepath)[:_BODY_LIMIT]
    except OSError:
        body = ""
    if len(body) < 80:
        body = article.description or article.title
    prompt = f"""你是 ToB 智能客服 / Agent 解决方案架构师。请阅读下面这篇 AI 公司博客，严格按四个角度提炼，用简体中文、务实、可落地，避免空话。

目标迁移场景：{_INDUSTRY_LENS}。

四个角度：
1. problem：他们面对的具体问题是什么？（业务痛点 / 技术瓶颈 / 约束，尽量具体）
2. method：采用了什么方法，为什么这样做？（关键设计选择与取舍）
3. validation：如何验证有效？需要什么业务和数据条件？（指标、实验设计、数据前提）
4. experiment：能否针对上述智能客服场景做一个「小实验」验证有效性？请给出：
   - 选哪个细分场景（呼入/外呼/文本 + 一个行业场景）
   - 最小可行实验设计（样本、周期、对照）
   - 成功指标
   - 若不适用，明确说明原因与更接近的替代场景

只返回 JSON：
{{
  "problem": "...",
  "method": "...",
  "validation": "...",
  "experiment": "..."
}}

每项 2-5 句，experiment 可稍长但控制在 120 字内。

标题：{article.title}
公司：{article.source_name}
链接：{article.url}

正文：
{body}
"""
    data = parse_llm_json(translator.complete(prompt))
    article.problem = _clean(data.get("problem"))
    article.method = _clean(data.get("method"))
    article.validation = _clean(data.get("validation"))
    article.experiment = _clean(data.get("experiment"))


def _synthesize_highlights(articles: list[SavedArticle], translator: Translator) -> str:
    bullets = [
        f"{i}. [{a.source_name}] {a.title}\n"
        f"   问题: {a.problem or '（未提炼）'}\n"
        f"   方法: {a.method or '（未提炼）'}\n"
        f"   验证: {a.validation or '（未提炼）'}\n"
        f"   实验: {a.experiment or '（未提炼）'}"
        for i, a in enumerate(articles[:20], 1)
    ]
    prompt = f"""根据以下公司博客的四维提炼，写一段「行业迁移启示」（4-7 句话，简体中文）。

读者是做 {_INDUSTRY_LENS} 的产品/解决方案同学。
要求：
- 归纳本次最值得迁移的方法或模式（不要复述每篇）
- 点出验证有效性时常见的业务/数据前提
- 给出 1-2 个本周就能开跑的小实验方向（呼入/外呼/文本任选，落到具体行业场景）
- 语气直接、可执行，像行动指南

只返回纯文本段落，不要标题、不要列表符号堆砌。

文章提炼：
{chr(10).join(bullets)}
"""
    return _clean(translator.complete(prompt))


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""

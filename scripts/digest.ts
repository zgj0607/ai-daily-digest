import { spawn } from 'node:child_process';
import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import process from 'node:process';
import {
  CheckpointStore,
  deserializeArticle,
  deserializeRanked,
  serializeArticle,
} from './checkpoint.ts';
import { saveArticleClippings } from './clippings.ts';
import { selectDiverseTopArticles, normalizeTopicSlug } from './diversity.ts';
import { generateDigestReport } from './report.ts';
import type {
  AIClient,
  Article,
  ArticleScoreBreakdown,
  CategoryId,
  FeedSource,
  RankedArticle,
  ScoredArticle,
} from './types.ts';
import { parseJsonResponse, parsePositiveInt, runWithConcurrency } from './utils.ts';

/** 爬取阶段结束后、真正调用大模型前，按需启动本地 LLM（由 DIGEST_ENSURE_LLM_CMD 指定） */
async function ensureLLMReady(): Promise<void> {
  const cmd = process.env.DIGEST_ENSURE_LLM_CMD?.trim();
  if (!cmd) return;

  console.log(`[digest] Ensuring LLM ready via: ${cmd}`);
  await new Promise<void>((resolve, reject) => {
    const child = spawn(cmd, {
      shell: true,
      stdio: 'inherit',
      env: process.env,
    });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`DIGEST_ENSURE_LLM_CMD exited with code ${code}`));
    });
  });
}

// ============================================================================
// Constants
// ============================================================================

const GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent';
const OPENAI_DEFAULT_API_BASE = 'https://api.openai.com/v1';
const OPENAI_DEFAULT_MODEL = 'gpt-4o-mini';
const FEED_FETCH_TIMEOUT_MS = 15_000;
const DEFAULT_FEED_CONCURRENCY = 50;
const DEFAULT_CLIPPINGS_DIR = '/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings';

/**
 * 本地 RTX 4090 (24GB) + Qwen3.6-27B / vLLM 调参要点：
 * - 单卡 KV cache 有限，并发过高只会排队、挤占显存，吞吐不升反降
 * - vLLM continuous batching：2~4 路并发 + 更大「单请求内文章数」更划算
 * - 评分输出短 → batch 可更大；摘要/翻译输出长 → batch 略小
 */
const LOCAL_LLM_PROFILE = {
  aiConcurrency: 3,
  scoreBatchSize: 10,
  summaryBatchSize: 6,
  clippingsConcurrency: 3,
  translateBatch: 8,
  translateConcurrency: 2,
} as const;

const CLOUD_LLM_PROFILE = {
  aiConcurrency: 6,
  scoreBatchSize: 12,
  summaryBatchSize: 8,
  clippingsConcurrency: 4,
  translateBatch: 8,
  translateConcurrency: 3,
} as const;

type LlmProfile = typeof LOCAL_LLM_PROFILE;

function isLocalApiBase(apiBase: string | undefined): boolean {
  const base = apiBase || '';
  return /127\.0\.0\.1|localhost|192\.168\.|10\.\d+\.|172\.(1[6-9]|2\d|3[01])\./.test(base);
}

function resolveLlmProfile(apiBase: string | undefined): LlmProfile {
  return isLocalApiBase(apiBase) ? LOCAL_LLM_PROFILE : CLOUD_LLM_PROFILE;
}

const RSS_FEEDS_FILE = new URL('../rss.txt', import.meta.url);

interface GeminiScoringResult {
  results: Array<{
    index: number;
    relevance: number;
    quality: number;
    timeliness: number;
    category: string;
    keywords: string[];
    topic?: string;
  }>;
}

interface GeminiSummaryResult {
  results: Array<{
    index: number;
    titleZh: string;
    summary: string;
    reason: string;
  }>;
}

function normalizeFeedUrl(url: string): string {
  return url
    .trim()
    .replace(/^https?:\/\//i, '')
    .replace(/\/+$/, '')
    .toLowerCase();
}

function parseFeedSource(feedUrl: string): FeedSource | null {
  try {
    const url = new URL(feedUrl);
    const host = url.hostname.replace(/^www\./, '');
    const segments = url.pathname.split('/').filter(Boolean);

    while (segments.length > 0) {
      const last = segments[segments.length - 1]!.toLowerCase();
      if (
        last === 'feed' ||
        last === 'rss' ||
        last === 'atom' ||
        last === 'default' ||
        last.endsWith('.xml') ||
        last.endsWith('.rss') ||
        last.endsWith('.atom')
      ) {
        segments.pop();
        continue;
      }
      break;
    }

    while (segments.length > 0) {
      const last = segments[segments.length - 1]!.toLowerCase();
      if (last === 'feeds' || last === 'posts') {
        segments.pop();
        continue;
      }
      break;
    }

    const sitePath = segments.length > 0 ? `/${segments.join('/')}` : '';
    const htmlUrl = `${url.origin}${sitePath}`;
    const name = `${host}${sitePath}`;

    return {
      name,
      xmlUrl: feedUrl,
      htmlUrl,
    };
  } catch {
    return null;
  }
}

async function loadRSSFeeds(): Promise<FeedSource[]> {
  const feeds: FeedSource[] = [];
  const seen = new Set<string>();

  try {
    const content = await readFile(RSS_FEEDS_FILE, 'utf8');

    for (const line of content.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;

      const normalized = normalizeFeedUrl(trimmed);
      if (!normalized || seen.has(normalized)) continue;

      const parsed = parseFeedSource(trimmed);
      if (!parsed) {
        console.warn(`[digest] Skip invalid RSS URL: ${trimmed}`);
        continue;
      }

      seen.add(normalized);
      feeds.push(parsed);
    }
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    console.warn(`[digest] Failed to load RSS feeds from rss.txt: ${msg}`);
  }

  return feeds;
}

// ============================================================================
// RSS/Atom Parsing (using Bun's built-in HTMLRewriter or manual XML parsing)
// ============================================================================

function stripHtml(html: string): string {
  return html
    .replace(/<[^>]*>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(parseInt(code)))
    .trim();
}

function extractCDATA(text: string): string {
  const cdataMatch = text.match(/<!\[CDATA\[([\s\S]*?)\]\]>/);
  return cdataMatch ? cdataMatch[1] : text;
}

function getTagContent(xml: string, tagName: string): string {
  // Handle namespaced and non-namespaced tags
  const patterns = [
    new RegExp(`<${tagName}[^>]*>([\\s\\S]*?)</${tagName}>`, 'i'),
    new RegExp(`<${tagName}[^>]*/>`, 'i'), // self-closing
  ];
  
  for (const pattern of patterns) {
    const match = xml.match(pattern);
    if (match?.[1]) {
      return extractCDATA(match[1]).trim();
    }
  }
  return '';
}

function getAttrValue(xml: string, tagName: string, attrName: string): string {
  const pattern = new RegExp(`<${tagName}[^>]*\\s${attrName}=["']([^"']*)["'][^>]*/?>`, 'i');
  const match = xml.match(pattern);
  return match?.[1] || '';
}

function parseDate(dateStr: string): Date | null {
  if (!dateStr) return null;
  
  const d = new Date(dateStr);
  if (!isNaN(d.getTime())) return d;
  
  // Try common RSS date formats
  // RFC 822: "Mon, 01 Jan 2024 00:00:00 GMT"
  const rfc822 = dateStr.match(/(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})/);
  if (rfc822) {
    const parsed = new Date(dateStr);
    if (!isNaN(parsed.getTime())) return parsed;
  }
  
  return null;
}

/** RSS 通常倒序排列；连续遇到这么多「明确过期」条目后停止解析该 feed */
const FEED_OLD_STREAK_STOP = 5;

function parseRSSItems(
  xml: string,
  cutoffMs?: number
): Array<{ title: string; link: string; pubDate: string; description: string }> {
  const items: Array<{ title: string; link: string; pubDate: string; description: string }> = [];
  let oldStreak = 0;
  let keptAny = false;

  const maybeKeep = (title: string, link: string, pubDate: string, description: string): boolean => {
    if (!title && !link) return true;
    if (cutoffMs !== undefined) {
      const parsed = parseDate(pubDate);
      if (parsed) {
        if (parsed.getTime() <= cutoffMs) {
          oldStreak++;
          // 已收录过新文且连续多条过期 → 视为倒序 feed，提前结束
          if (keptAny && oldStreak >= FEED_OLD_STREAK_STOP) return false;
          return true;
        }
        oldStreak = 0;
      }
    }
    items.push({ title, link, pubDate, description: description.slice(0, 500) });
    keptAny = true;
    return true;
  };

  const isAtom = xml.includes('<feed') && xml.includes('xmlns="http://www.w3.org/2005/Atom"') || xml.includes('<feed ');

  if (isAtom) {
    const entryPattern = /<entry[\s>]([\s\S]*?)<\/entry>/gi;
    let entryMatch;
    while ((entryMatch = entryPattern.exec(xml)) !== null) {
      const entryXml = entryMatch[1];
      const title = stripHtml(getTagContent(entryXml, 'title'));
      let link = getAttrValue(entryXml, 'link[^>]*rel="alternate"', 'href');
      if (!link) link = getAttrValue(entryXml, 'link', 'href');
      const pubDate = getTagContent(entryXml, 'published') || getTagContent(entryXml, 'updated');
      const description = stripHtml(
        getTagContent(entryXml, 'summary') || getTagContent(entryXml, 'content')
      );
      if (!maybeKeep(title, link, pubDate, description)) break;
    }
  } else {
    const itemPattern = /<item[\s>]([\s\S]*?)<\/item>/gi;
    let itemMatch;
    while ((itemMatch = itemPattern.exec(xml)) !== null) {
      const itemXml = itemMatch[1];
      const title = stripHtml(getTagContent(itemXml, 'title'));
      const link = getTagContent(itemXml, 'link') || getTagContent(itemXml, 'guid');
      const pubDate =
        getTagContent(itemXml, 'pubDate')
        || getTagContent(itemXml, 'dc:date')
        || getTagContent(itemXml, 'date');
      const description = stripHtml(
        getTagContent(itemXml, 'description') || getTagContent(itemXml, 'content:encoded')
      );
      if (!maybeKeep(title, link, pubDate, description)) break;
    }
  }

  return items;
}

// ============================================================================
// Feed Fetching
// ============================================================================

async function fetchFeed(feed: FeedSource, cutoffMs: number): Promise<Article[]> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), FEED_FETCH_TIMEOUT_MS);

    const response = await fetch(feed.xmlUrl, {
      signal: controller.signal,
      headers: {
        'User-Agent': 'AI-Daily-Digest/1.0 (RSS Reader)',
        'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*',
      },
    });

    clearTimeout(timeout);

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const xml = await response.text();
    // 解析时即按时间窗口过滤（RSS 协议本身无法只请求「最新一天」）
    const items = parseRSSItems(xml, cutoffMs);

    return items
      .map(item => ({
        title: item.title,
        link: item.link,
        pubDate: parseDate(item.pubDate) || new Date(0),
        description: item.description,
        sourceName: feed.name,
        sourceUrl: feed.htmlUrl,
      }))
      .filter(a => a.pubDate.getTime() > cutoffMs);
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    if (!msg.includes('abort')) {
      console.warn(`[digest] ✗ ${feed.name}: ${msg}`);
    } else {
      console.warn(`[digest] ✗ ${feed.name}: timeout`);
    }
    return [];
  }
}

async function fetchAllFeeds(
  feeds: FeedSource[],
  concurrency: number,
  cutoffMs: number,
  checkpoint?: CheckpointStore
): Promise<{ articles: Article[]; successCount: number; failCount: number }> {
  const doneUrls = new Set(checkpoint?.state.crawl.doneFeedXmlUrls || []);
  const allArticles: Article[] = (checkpoint?.state.crawl.articles || []).map(deserializeArticle);
  let successCount = checkpoint?.state.crawl.successCount || 0;
  let failCount = checkpoint?.state.crawl.failCount || 0;

  const pending = feeds.filter(f => !doneUrls.has(f.xmlUrl));
  if (doneUrls.size > 0) {
    console.log(`[digest] Crawl resume: ${doneUrls.size} feeds done, ${pending.length} remaining, ${allArticles.length} articles cached`);
  }

  if (pending.length === 0) {
    if (checkpoint && !checkpoint.state.crawl.complete) {
      await checkpoint.updateCrawl(c => { c.complete = true; }, true);
    }
    return { articles: allArticles, successCount, failCount };
  }

  let completedCount = doneUrls.size;
  let nextIndex = 0;
  const workerCount = Math.min(concurrency, pending.length);
  const progressInterval = Math.max(10, Math.ceil(feeds.length / 20));
  const totalFeeds = feeds.length;

  async function worker(): Promise<void> {
    while (true) {
      const currentIndex = nextIndex++;
      if (currentIndex >= pending.length) return;

      const feed = pending[currentIndex]!;
      const articles = await fetchFeed(feed, cutoffMs);
      if (articles.length > 0) {
        allArticles.push(...articles);
        successCount++;
      } else {
        failCount++;
      }

      if (checkpoint) {
        await checkpoint.updateCrawl(c => {
          c.doneFeedXmlUrls.push(feed.xmlUrl);
          c.articles.push(...articles.map(serializeArticle));
          c.successCount = successCount;
          c.failCount = failCount;
        });
      }

      completedCount++;
      if (completedCount % progressInterval === 0 || completedCount === totalFeeds) {
        console.log(`[digest] Progress: ${completedCount}/${totalFeeds} feeds (${successCount} with recent, ${failCount} empty/failed)`);
      }
    }
  }

  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  if (checkpoint) {
    await checkpoint.updateCrawl(c => { c.complete = true; }, true);
  }
  console.log(`[digest] Kept ${allArticles.length} articles within window from ${successCount} feeds (${failCount} empty/failed)`);
  return { articles: allArticles, successCount, failCount };
}

// ============================================================================
// AI Providers (Gemini + OpenAI-compatible fallback)
// ============================================================================

async function callGemini(prompt: string, apiKey: string): Promise<string> {
  const response = await fetch(`${GEMINI_API_URL}?key=${apiKey}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig: {
        temperature: 0.3,
        topP: 0.8,
        topK: 40,
      },
    }),
  });
  
  if (!response.ok) {
    const errorText = await response.text().catch(() => 'Unknown error');
    throw new Error(`Gemini API error (${response.status}): ${errorText}`);
  }
  
  const data = await response.json() as {
    candidates?: Array<{
      content?: { parts?: Array<{ text?: string }> };
    }>;
  };
  
  return data.candidates?.[0]?.content?.parts?.[0]?.text || '';
}

async function callOpenAICompatible(
  prompt: string,
  apiKey: string,
  apiBase: string,
  model: string
): Promise<string> {
  const normalizedBase = apiBase.replace(/\/+$/, '');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 300_000); // 300 second timeout (5 min for large batches on local GPU)
  
  try {
    const response = await fetch(`${normalizedBase}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: prompt }],
        temperature: 0.3,
        top_p: 0.8,
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const errorText = await response.text().catch(() => 'Unknown error');
      throw new Error(`OpenAI-compatible API error (${response.status}): ${errorText}`);
    }

    const data = await response.json() as {
      choices?: Array<{
        message?: {
          content?: string | Array<{ type?: string; text?: string }>;
          reasoning_content?: string;
        };
      }>;
    };

    const content = data.choices?.[0]?.message?.content;
    if (typeof content === 'string' && content.length > 0) return content;
    if (Array.isArray(content)) {
      const text = content
        .filter(item => item.type === 'text' && typeof item.text === 'string')
        .map(item => item.text)
        .join('\n');
      if (text.length > 0) return text;
    }
    // Fallback: some reasoning models put output in reasoning_content
    const reasoning = data.choices?.[0]?.message?.reasoning_content;
    if (typeof reasoning === 'string' && reasoning.length > 0) return reasoning;
    return '';
  } finally {
    clearTimeout(timeout);
  }
}

function inferOpenAIModel(apiBase: string): string {
  const base = apiBase.toLowerCase();
  if (base.includes('deepseek')) return 'deepseek-chat';
  return OPENAI_DEFAULT_MODEL;
}

interface TranslateBackend {
  name: string;
  apiKey: string;
  apiBase: string;
  model: string;
  cloud?: boolean;
}

function isRateLimited(reason: string): boolean {
  return /429|rate limit|ratelimit|too many requests|throttl|quota|限流|请求频率|requestlimit/i.test(reason);
}

/** 剪藏翻译：本地 Qwen 与云端千问按空闲程度同时接请求。云端限流后关闭该通道。 */
function createClippingTranslateClient(backends: TranslateBackend[], fallback: AIClient): AIClient {
  const pool = backends.filter(item => item.apiKey && item.apiBase && item.model);
  if (!pool.length) return fallback;
  const inflight = pool.map(() => 0);
  let cloudClosed = false;
  console.log(`[digest] 剪藏翻译通道: ${pool.map(item => item.name).join(' | ')}`);
  return {
    async call(prompt: string): Promise<string> {
      const order = pool
        .map((_, index) => index)
        .filter(index => !(pool[index]!.cloud && cloudClosed))
        .sort((a, b) => inflight[a]! - inflight[b]!);
      const errors: string[] = [];
      for (const index of order) {
        const backend = pool[index]!;
        if (backend.cloud && cloudClosed) continue;
        inflight[index]! += 1;
        try {
          const text = await callOpenAICompatible(prompt, backend.apiKey, backend.apiBase, backend.model);
          if (!text.trim()) throw new Error('返回内容为空');
          return text;
        } catch (error) {
          const reason = error instanceof Error ? error.message : String(error);
          errors.push(`${backend.name}: ${reason}`);
          if (backend.cloud && isRateLimited(reason)) {
            if (!cloudClosed) {
              cloudClosed = true;
              console.warn(`[digest] 云端千问触发限流（5小时6000次），后续翻译改走本地模型。原因: ${reason}`);
            }
          } else {
            console.warn(`[digest] ${backend.name} 翻译失败，改试其他通道。原因: ${reason}`);
          }
        } finally {
          inflight[index]! -= 1;
        }
      }
      try {
        return await fallback.call(prompt);
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        throw new Error(`所有翻译接口都失败: ${errors.join(' | ')} | fallback: ${reason}`);
      }
    },
  };
}

function createAIClient(config: {
  geminiApiKey?: string;
  openaiApiKey?: string;
  openaiApiBase?: string;
  openaiModel?: string;
}): AIClient {
  const state = {
    geminiApiKey: config.geminiApiKey?.trim() || '',
    openaiApiKey: config.openaiApiKey?.trim() || '',
    openaiApiBase: (config.openaiApiBase?.trim() || OPENAI_DEFAULT_API_BASE).replace(/\/+$/, ''),
    openaiModel: config.openaiModel?.trim() || '',
    geminiEnabled: Boolean(config.geminiApiKey?.trim()),
    fallbackLogged: false,
  };

  if (!state.openaiModel) {
    state.openaiModel = inferOpenAIModel(state.openaiApiBase);
  }

  return {
    async call(prompt: string): Promise<string> {
      if (state.geminiEnabled && state.geminiApiKey) {
        try {
          return await callGemini(prompt, state.geminiApiKey);
        } catch (error) {
          if (state.openaiApiKey) {
            if (!state.fallbackLogged) {
              const reason = error instanceof Error ? error.message : String(error);
              console.warn(`[digest] Gemini failed, switching to OpenAI-compatible fallback (${state.openaiApiBase}, model=${state.openaiModel}). Reason: ${reason}`);
              state.fallbackLogged = true;
            }
            state.geminiEnabled = false;
            return callOpenAICompatible(prompt, state.openaiApiKey, state.openaiApiBase, state.openaiModel);
          }
          throw error;
        }
      }

      if (state.openaiApiKey) {
        return callOpenAICompatible(prompt, state.openaiApiKey, state.openaiApiBase, state.openaiModel);
      }

      throw new Error('No AI API key configured. Set GEMINI_API_KEY and/or OPENAI_API_KEY.');
    },
  };
}

// ============================================================================
// AI Scoring
// ============================================================================

function buildScoringPrompt(articles: Array<{ index: number; title: string; description: string; sourceName: string }>): string {
  const articlesList = articles.map(a =>
    `Index ${a.index}: [${a.sourceName}] ${a.title}\n${a.description.slice(0, 300)}`
  ).join('\n\n---\n\n');

  return `你是一个技术内容策展人，正在为一份面向技术爱好者的每日精选摘要筛选文章。

请对以下文章进行三个维度的评分（1-10 整数，10 分最高），并为每篇文章分配一个分类标签和提取 2-4 个关键词。

## 评分维度

### 1. 相关性 (relevance) - 对技术/编程/AI/互联网从业者的价值
- 10: 所有技术人都应该知道的重大事件/突破
- 7-9: 对大部分技术从业者有价值
- 4-6: 对特定技术领域有价值
- 1-3: 与技术行业关联不大

### 2. 质量 (quality) - 文章本身的深度和写作质量
- 10: 深度分析，原创洞见，引用丰富
- 7-9: 有深度，观点独到
- 4-6: 信息准确，表达清晰
- 1-3: 浅尝辄止或纯转述

### 3. 时效性 (timeliness) - 当前是否值得阅读
- 10: 正在发生的重大事件/刚发布的重要工具
- 7-9: 近期热点相关
- 4-6: 常青内容，不过时
- 1-3: 过时或无时效价值

## 分类标签（必须从以下选一个）
- ai-ml: AI、机器学习、LLM、深度学习相关
- security: 安全、隐私、漏洞、加密相关
- engineering: 软件工程、架构、编程语言、系统设计
- tools: 开发工具、开源项目、新发布的库/框架
- opinion: 行业观点、个人思考、职业发展、文化评论
- other: 以上都不太适合的

## 关键词提取
提取 2-4 个最能代表文章主题的关键词（用英文，简短，如 "Rust", "LLM", "database", "performance"）

## 主题标识 (topic)
用 2-4 个英文单词的 slug 标识文章所讨论的核心事件或话题（如 "openai-gpt5-release", "rust-async-io", "kubernetes-1-33"）。
- 多篇报道**同一新闻事件/产品发布/漏洞**时，必须使用**相同**的 topic
- 不要每篇文章都写不同的 topic，同一热点应归并
- 常青教程、与具体事件无关的内容用描述性 slug（如 "postgres-indexing-guide"）

## 待评分文章

${articlesList}

请严格按 JSON 格式返回，不要包含 markdown 代码块或其他文字：
{
  "results": [
    {
      "index": 0,
      "relevance": 8,
      "quality": 7,
      "timeliness": 9,
      "category": "engineering",
      "keywords": ["Rust", "compiler", "performance"],
      "topic": "rust-compiler-optimization"
    }
  ]
}`;
}

async function scoreArticlesWithAI(
  articles: Article[],
  aiClient: AIClient,
  aiConcurrency: number,
  batchSize: number,
  checkpoint?: CheckpointStore
): Promise<Map<number, ArticleScoreBreakdown>> {
  const allScores = new Map<number, ArticleScoreBreakdown>();
  const cached = checkpoint?.state.score.byIndex || {};
  for (const [key, value] of Object.entries(cached)) {
    allScores.set(Number(key), value);
  }

  const indexed = articles.map((article, index) => ({
    index,
    title: article.title,
    description: article.description,
    sourceName: article.sourceName,
  }));

  const batches: typeof indexed[] = [];
  for (let i = 0; i < indexed.length; i += batchSize) {
    batches.push(indexed.slice(i, i + batchSize));
  }

  const pendingBatchIndices = batches
    .map((_, batchIndex) => batchIndex)
    .filter(batchIndex => {
      const batch = batches[batchIndex]!;
      return batch.some(item => !allScores.has(item.index));
    });

  console.log(
    `[digest] AI scoring: ${articles.length} articles in ${batches.length} batches` +
    (pendingBatchIndices.length < batches.length
      ? ` (resume: ${batches.length - pendingBatchIndices.length} done, ${pendingBatchIndices.length} left)`
      : '')
  );

  const validCategories = new Set<string>(['ai-ml', 'security', 'engineering', 'tools', 'opinion', 'other']);
  let completedBatches = batches.length - pendingBatchIndices.length;

  await runWithConcurrency(pendingBatchIndices.length, aiConcurrency, async (jobIndex) => {
    const batchIndex = pendingBatchIndices[jobIndex]!;
    const batch = batches[batchIndex]!;
    try {
      const prompt = buildScoringPrompt(batch);
      const responseText = await aiClient.call(prompt);
      const parsed = parseJsonResponse<GeminiScoringResult>(responseText);
      const batchScores = new Map<number, ArticleScoreBreakdown>();

      if (parsed.results && Array.isArray(parsed.results)) {
        for (const result of parsed.results) {
          const clamp = (v: number) => Math.min(10, Math.max(1, Math.round(v)));
          const cat = (validCategories.has(result.category) ? result.category : 'other') as CategoryId;
          batchScores.set(result.index, {
            relevance: clamp(result.relevance),
            quality: clamp(result.quality),
            timeliness: clamp(result.timeliness),
            category: cat,
            keywords: Array.isArray(result.keywords) ? result.keywords.slice(0, 4) : [],
            topic: normalizeTopicSlug(result.topic),
          });
        }
      }

      for (const [index, score] of batchScores) {
        allScores.set(index, score);
      }
      if (checkpoint && batchScores.size > 0) {
        await checkpoint.updateScore(s => {
          for (const [index, score] of batchScores) {
            s.byIndex[String(index)] = score;
          }
        });
      }
    } catch (error) {
      // 不写入默认分，便于断点重试该 batch
      console.warn(`[digest] Scoring batch failed (will retry on resume): ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      completedBatches++;
      console.log(`[digest] Scoring progress: ${completedBatches}/${batches.length} batches`);
    }
  });

  // 仍缺失的条目填默认分（保证下游可继续），并写入 checkpoint
  for (const item of indexed) {
    if (!allScores.has(item.index)) {
      const fallback: ArticleScoreBreakdown = {
        relevance: 5, quality: 5, timeliness: 5, category: 'other', keywords: [], topic: 'misc',
      };
      allScores.set(item.index, fallback);
      if (checkpoint) {
        await checkpoint.updateScore(s => { s.byIndex[String(item.index)] = fallback; });
      }
    }
  }

  if (checkpoint) {
    await checkpoint.updateScore(s => { s.complete = true; }, true);
    await checkpoint.flush();
  }
  return allScores;
}

// ============================================================================
// AI Summarization
// ============================================================================

function buildSummaryPrompt(
  articles: Array<{ index: number; title: string; description: string; sourceName: string; link: string }>,
  lang: 'zh' | 'en'
): string {
  const articlesList = articles.map(a =>
    `Index ${a.index}: [${a.sourceName}] ${a.title}\nURL: ${a.link}\n${a.description.slice(0, 800)}`
  ).join('\n\n---\n\n');

  const langInstruction = lang === 'zh'
    ? '请用中文撰写摘要和推荐理由。如果原文是英文，请翻译为中文。标题翻译也用中文。'
    : 'Write summaries, reasons, and title translations in English.';

  return `你是一个务实的技术内容编辑，读者是每天需要落地执行的工程师/产品经理。请为以下文章完成三件事：

1. **中文标题** (titleZh): 将英文标题翻译成自然的中文。如果原标题已经是中文则保持不变。
2. **摘要** (summary): 3-5 句话，聚焦「读完能马上做什么」，包含：
   - 文章解决的具体问题或场景（1 句，说清「谁、在什么情况下、遇到什么问题」）
   - 作者给出的核心方法、工具、命令、配置或步骤（1-2 句，保留具体名称和关键数字）
   - 读者今天/本周可以立刻尝试的 1-2 个行动点（用「可以…」「建议…」「试试…」开头）
3. **推荐理由** (reason): 1 句话说明「读完后你能立刻改变什么」，例如节省时间、避免踩坑、学到可复用的技巧。

${langInstruction}

写作要求：
- 语气平实、具体，像同事分享经验，不要写行业宏大叙事或「颠覆性」「革命性」等空话
- 直接说重点，禁止「本文讨论了…」「这篇文章介绍了…」开头
- 优先写可操作的细节：工具名、版本号、命令、参数、对比结论、适用/不适用场景
- 如果文章偏观点类，也要提炼出「你可以怎么调整自己的工作方式」
- 目标：读者 30 秒内判断「今天要不要做这件事、怎么做第一步」

## 待摘要文章

${articlesList}

请严格按 JSON 格式返回：
{
  "results": [
    {
      "index": 0,
      "titleZh": "中文翻译的标题",
      "summary": "摘要内容...",
      "reason": "推荐理由..."
    }
  ]
}`;
}

async function summarizeArticles(
  articles: Array<Article & { index: number }>,
  aiClient: AIClient,
  lang: 'zh' | 'en',
  aiConcurrency: number,
  batchSize: number,
  checkpoint?: CheckpointStore
): Promise<Map<number, { titleZh: string; summary: string; reason: string }>> {
  const summaries = new Map<number, { titleZh: string; summary: string; reason: string }>();
  for (const [key, value] of Object.entries(checkpoint?.state.summaries || {})) {
    summaries.set(Number(key), value);
  }

  const indexed = articles.map(a => ({
    index: a.index,
    title: a.title,
    description: a.description,
    sourceName: a.sourceName,
    link: a.link,
  }));

  const batches: typeof indexed[] = [];
  for (let i = 0; i < indexed.length; i += batchSize) {
    batches.push(indexed.slice(i, i + batchSize));
  }

  const pendingBatchIndices = batches
    .map((_, batchIndex) => batchIndex)
    .filter(batchIndex => batches[batchIndex]!.some(item => !summaries.has(item.index)));

  console.log(
    `[digest] Generating summaries for ${articles.length} articles in ${batches.length} batches` +
    (pendingBatchIndices.length < batches.length
      ? ` (resume: ${batches.length - pendingBatchIndices.length} done, ${pendingBatchIndices.length} left)`
      : '')
  );

  let completedBatches = batches.length - pendingBatchIndices.length;
  await runWithConcurrency(pendingBatchIndices.length, aiConcurrency, async (jobIndex) => {
    const batchIndex = pendingBatchIndices[jobIndex]!;
    const batch = batches[batchIndex]!;
    try {
      const prompt = buildSummaryPrompt(batch, lang);
      const responseText = await aiClient.call(prompt);
      const parsed = parseJsonResponse<GeminiSummaryResult>(responseText);

      if (parsed.results && Array.isArray(parsed.results)) {
        for (const result of parsed.results) {
          const value = {
            titleZh: result.titleZh || '',
            summary: result.summary || '',
            reason: result.reason || '',
          };
          summaries.set(result.index, value);
          if (checkpoint) await checkpoint.setSummary(result.index, value);
        }
      }
    } catch (error) {
      console.warn(`[digest] Summary batch failed (will retry on resume): ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      completedBatches++;
      console.log(`[digest] Summary progress: ${completedBatches}/${batches.length} batches`);
    }
  });

  for (const item of indexed) {
    if (!summaries.has(item.index)) {
      const value = { titleZh: item.title, summary: item.title, reason: '' };
      summaries.set(item.index, value);
      if (checkpoint) await checkpoint.setSummary(item.index, value);
    }
  }

  if (checkpoint) {
    checkpoint.state.summarizeComplete = true;
    await checkpoint.flush();
  }
  return summaries;
}

// ============================================================================
// AI Highlights (Today's Trends)
// ============================================================================

async function generateHighlights(
  articles: ScoredArticle[],
  aiClient: AIClient,
  lang: 'zh' | 'en'
): Promise<string> {
  const articleList = articles.slice(0, 10).map((a, i) =>
    `${i + 1}. [${a.category}] ${a.titleZh || a.title} — ${a.summary.slice(0, 100)}`
  ).join('\n');

  const langNote = lang === 'zh' ? '用中文回答。' : 'Write in English.';

  const prompt = `根据以下今日精选技术文章列表，写一段「今日行动指南」（3-5 句话）。
要求：
- 不要写宏观行业趋势或空泛判断，聚焦读者今天/本周能落地的具体行动
- 归纳出 2-3 个可执行方向，每个方向说清楚：做什么、用什么工具/方法、解决什么问题
- 如果多篇文章指向同一实践（如某工具、某架构模式），合并成一条建议，避免重复
- 语气像靠谱同事的快速同步：短句、具体、能直接照着做
- 禁止「AI 正在重塑…」「行业迎来拐点…」这类宏大表述
${langNote}

文章列表：
${articleList}

直接返回纯文本，不要 JSON，不要 markdown 格式。`;

  try {
    const text = await aiClient.call(prompt);
    return text.trim();
  } catch (error) {
    console.warn(`[digest] Highlights generation failed: ${error instanceof Error ? error.message : String(error)}`);
    return '';
  }
}

// ============================================================================
// CLI
// ============================================================================

function printUsage(): void {
  console.log(`AI Daily Digest - AI-powered RSS digest from rss.txt

Usage:
  bun scripts/digest.ts [options]

Options:
  --hours <n>     Time range in hours (default: 48)
  --top-n <n>     Number of top articles to include (default: 30)
  --lang <lang>   Summary language: zh or en (default: zh)
  --feed-concurrency <n> Max concurrent RSS fetches (default: 50)
  --ai-concurrency <n> Max concurrent AI requests (local 4090 default: 3)
  --score-batch <n>    Articles per scoring request (local default: 10)
  --summary-batch <n>  Articles per summary request (local default: 6)
  --clippings-dir <path> Directory to save full article markdown
  --clippings-concurrency <n> Concurrent clipping jobs (local default: 3)
  --output <path> Output file path (default: ./digest-YYYYMMDD.md)
  --fresh         Ignore existing checkpoint and start a new run
  --help          Show this help

Environment:
  GEMINI_API_KEY   Optional but recommended. Get one at https://aistudio.google.com/apikey
  OPENAI_API_KEY   Optional fallback key for OpenAI-compatible APIs
  OPENAI_API_BASE  Optional fallback base URL (default: https://api.openai.com/v1)
  OPENAI_MODEL     Optional fallback model (default: deepseek-chat for DeepSeek base, else gpt-4o-mini)
  FEED_CONCURRENCY Optional RSS fetch concurrency override
  AI_CONCURRENCY / SCORE_BATCH / SUMMARY_BATCH / CLIPPINGS_CONCURRENCY
  DIGEST_CACHE_DIR Optional checkpoint directory (default: .cache/digest)
  DIGEST_ENSURE_LLM_CMD  Optional shell command run after crawl, before AI scoring

Checkpoint:
  默认自动断点续跑。进度保存在 .cache/digest/checkpoint-*.json
  中断后用相同 --hours/--top-n/--lang 再跑即可从断点继续；--fresh 强制重来。

Examples:
  bun scripts/digest.ts --hours 24 --top-n 30 --lang zh
  bun scripts/digest.ts --hours 24 --top-n 30 --lang zh   # resume after crash
  bun scripts/digest.ts --fresh --hours 24 --top-n 30 --lang zh
`);
  process.exit(0);
}

function prefilterForLocalLLM(recentArticles: Article[], topN: number): Article[] {
  if (recentArticles.length <= topN * 2) return recentArticles;
  console.log(`[digest] Pre-filtering ${recentArticles.length} articles for local LLM (target: ~${topN * 2})...`);
  const techKeywords = /\b(ai|llm|gpt|claude|gemini|machine learning|deep learning|neural|transformer|diffusion|agent|rag|embedding|vector|python|rust|golang|typescript|javascript|kubernetes|docker|linux|security|vulnerability|exploit|database|postgres|redis|api|grpc|websocket|performance|optimization|scalability|distributed|blockchain|crypto|web3|devops|ci\/cd|git|open source|oss|framework|library|sdk|cli|gui|ui\/ux|frontend|backend|fullstack|cloud|aws|azure|gcp|serverless|microservice|architecture|algorithm|data structure|compiler|interpreter|browser|web|mobile|ios|android)\b/i;

  const scored = recentArticles.map((article, idx) => {
    let score = 0;
    const text = `${article.title} ${article.description}`.toLowerCase();
    if (techKeywords.test(text)) score += 3;
    if (article.title.length > 20) score += 1;
    if (article.description.length > 100) score += 1;
    if (/\b(new|release|launch|announce|update|fix|bug|vulnerability|breakthrough|introducing)\b/i.test(article.title)) score += 2;
    return { idx, score };
  });

  scored.sort((a, b) => b.score - a.score);
  const preFilterCount = Math.min(topN * 2, recentArticles.length);
  const selectedIndices = new Set(scored.slice(0, preFilterCount).map(s => s.idx));
  const filtered = recentArticles.filter((_, idx) => selectedIndices.has(idx));
  console.log(`[digest] Pre-filtered to ${filtered.length} candidates for AI scoring`);
  return filtered;
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes('--help') || args.includes('-h')) printUsage();

  let hours = 48;
  let topN = 30;
  let lang: 'zh' | 'en' = 'zh';
  let outputPath = '';
  let fresh = false;
  let clippingsDir = process.env.CLIPPINGS_DIR?.trim() || DEFAULT_CLIPPINGS_DIR;
  let feedConcurrency = parsePositiveInt(process.env.FEED_CONCURRENCY, DEFAULT_FEED_CONCURRENCY);
  let aiConcurrency: number | undefined;
  let scoreBatch: number | undefined;
  let summaryBatch: number | undefined;
  let clippingsConcurrency: number | undefined;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i]!;
    if (arg === '--hours' && args[i + 1]) {
      hours = parseInt(args[++i]!, 10);
    } else if (arg === '--top-n' && args[i + 1]) {
      topN = parseInt(args[++i]!, 10);
    } else if (arg === '--lang' && args[i + 1]) {
      lang = args[++i] as 'zh' | 'en';
    } else if (arg === '--feed-concurrency' && args[i + 1]) {
      feedConcurrency = parsePositiveInt(args[++i], feedConcurrency);
    } else if (arg === '--ai-concurrency' && args[i + 1]) {
      aiConcurrency = parsePositiveInt(args[++i], 3);
    } else if (arg === '--score-batch' && args[i + 1]) {
      scoreBatch = parsePositiveInt(args[++i], 10);
    } else if (arg === '--summary-batch' && args[i + 1]) {
      summaryBatch = parsePositiveInt(args[++i], 6);
    } else if (arg === '--clippings-dir' && args[i + 1]) {
      clippingsDir = args[++i]!;
    } else if (arg === '--clippings-concurrency' && args[i + 1]) {
      clippingsConcurrency = parsePositiveInt(args[++i], 3);
    } else if (arg === '--output' && args[i + 1]) {
      outputPath = args[++i]!;
    } else if (arg === '--fresh') {
      fresh = true;
    }
  }

  const geminiApiKey = process.env.GEMINI_API_KEY;
  const localApiKey = process.env.LOCAL_LLM_API_KEY?.trim() || '';
  const localApiBase = (process.env.LOCAL_LLM_API_BASE?.trim() || '').replace(/\/+$/, '');
  const localModel = process.env.LOCAL_LLM_MODEL?.trim() || '';
  const cloudApiKey = process.env.OPENAI_API_KEY?.trim() || '';
  const cloudApiBase = (process.env.OPENAI_API_BASE?.trim() || '').replace(/\/+$/, '');
  const cloudModel = (process.env.OPENAI_MODEL || '').trim();
  const scoringKey = localApiKey || cloudApiKey;
  const scoringBase = localApiBase || cloudApiBase || OPENAI_DEFAULT_API_BASE;
  const scoringModel = localModel || cloudModel;

  const profile = resolveLlmProfile(scoringBase);
  aiConcurrency = aiConcurrency
    ?? parsePositiveInt(process.env.AI_CONCURRENCY, profile.aiConcurrency);
  scoreBatch = scoreBatch
    ?? parsePositiveInt(process.env.SCORE_BATCH, profile.scoreBatchSize);
  summaryBatch = summaryBatch
    ?? parsePositiveInt(process.env.SUMMARY_BATCH, profile.summaryBatchSize);
  clippingsConcurrency = clippingsConcurrency
    ?? parsePositiveInt(process.env.CLIPPINGS_CONCURRENCY, profile.clippingsConcurrency);

  if (!geminiApiKey && !scoringKey) {
    console.error('[digest] Error: Missing API key. Set LOCAL_LLM_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY.');
    console.error('[digest] Gemini key: https://aistudio.google.com/apikey');
    process.exit(1);
  }

  const aiClient = createAIClient({
    geminiApiKey,
    openaiApiKey: scoringKey,
    openaiApiBase: scoringBase,
    openaiModel: scoringModel,
  });

  if (!outputPath) {
    const dateStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    outputPath = `./digest-${dateStr}.md`;
  }

  const checkpoint = await CheckpointStore.open({ hours, topN, lang, fresh });

  console.log(`[digest] === AI Daily Digest ===`);
  console.log(`[digest] Time range: ${hours} hours`);
  console.log(`[digest] Top N: ${topN}`);
  console.log(`[digest] Language: ${lang}`);
  console.log(`[digest] Feed concurrency: ${feedConcurrency}`);
  console.log(`[digest] LLM profile: ${isLocalApiBase(scoringBase) ? 'local RTX4090/Qwen' : 'cloud'}`);
  console.log(`[digest] AI concurrency: ${aiConcurrency} | score-batch: ${scoreBatch} | summary-batch: ${summaryBatch}`);
  console.log(`[digest] Clippings concurrency: ${clippingsConcurrency}`);
  console.log(`[digest] Clippings dir: ${clippingsDir}`);
  console.log(`[digest] Output: ${outputPath}`);
  console.log(`[digest] Stage: ${checkpoint.state.stage}`);
  console.log(`[digest] AI provider: ${geminiApiKey ? 'Gemini (primary)' : 'OpenAI-compatible (primary)'}`);
  console.log(`[digest] 评分模型: ${scoringBase} (model=${scoringModel || inferOpenAIModel(scoringBase)})`);
  if (cloudApiKey && cloudApiBase && cloudApiBase !== localApiBase) {
    console.log(`[digest] 云端翻译: ${cloudApiBase} (model=${cloudModel || 'qwen'})`);
  }
  console.log('');

  const rssFeeds = await loadRSSFeeds();
  const cutoffMs = Date.now() - hours * 60 * 60 * 1000;
  console.log(`[digest] RSS sources loaded from rss.txt: ${rssFeeds.length}`);

  // --- Stage: crawl ---
  let recentArticles: Article[];
  let successCount: number;
  if (checkpoint.state.crawl.complete) {
    recentArticles = checkpoint.state.crawl.articles.map(deserializeArticle);
    successCount = checkpoint.state.crawl.successCount;
    console.log(`[digest] Step 1/5: Crawl cached (${recentArticles.length} articles, skip fetch)`);
  } else {
    await checkpoint.setStage('crawl');
    console.log(`[digest] Step 1/5: Fetching feeds (keep last ${hours}h only, parse-time filter)...`);
    const result = await fetchAllFeeds(rssFeeds, feedConcurrency, cutoffMs, checkpoint);
    recentArticles = result.articles;
    successCount = result.successCount;
  }

  if (recentArticles.length === 0) {
    console.error(`[digest] Error: No articles found within the last ${hours} hours.`);
    console.error(`[digest] Try increasing --hours (e.g., --hours 168 for one week)`);
    process.exit(1);
  }

  const byLink = new Map(recentArticles.map(a => [a.link, a]));
  const isLocalLLM = isLocalApiBase(scoringBase);

  let articlesToScore: Article[];
  if (checkpoint.state.score.candidateLinks.length > 0) {
    articlesToScore = checkpoint.state.score.candidateLinks
      .map(link => byLink.get(link))
      .filter((a): a is Article => Boolean(a));
    console.log(`[digest] Score candidates restored: ${articlesToScore.length}`);
  } else {
    articlesToScore = isLocalLLM ? prefilterForLocalLLM(recentArticles, topN) : recentArticles;
    await checkpoint.updateScore(s => {
      s.candidateLinks = articlesToScore.map(a => a.link);
    }, true);
  }

  const needsLLM =
    !checkpoint.state.score.complete
    || !checkpoint.state.summarizeComplete
    || !checkpoint.state.highlightsComplete
    || !checkpoint.state.clippingsComplete;

  if (needsLLM) {
    await ensureLLMReady();
  }

  // --- Stage: score ---
  let scores: Map<number, ArticleScoreBreakdown>;
  if (checkpoint.state.score.complete) {
    scores = new Map(
      Object.entries(checkpoint.state.score.byIndex).map(([k, v]) => [Number(k), v])
    );
    console.log(`[digest] Step 2/5: Scoring cached (${scores.size} scores, skip AI)`);
  } else {
    await checkpoint.setStage('score');
    console.log(`[digest] Step 2/5: AI scoring ${articlesToScore.length} articles...`);
    scores = await scoreArticlesWithAI(articlesToScore, aiClient, aiConcurrency, scoreBatch, checkpoint);
  }

  const articleScoreMap = new Map<string, ArticleScoreBreakdown>();
  articlesToScore.forEach((article, position) => {
    const score = scores.get(position);
    if (score) articleScoreMap.set(article.link, score);
  });

  const scoredArticles: RankedArticle[] = recentArticles.map((article, index) => {
    const score = articleScoreMap.get(article.link)
      || (articlesToScore === recentArticles
        ? scores.get(index)
        : undefined)
      || { relevance: 5, quality: 5, timeliness: 5, category: 'other' as CategoryId, keywords: [], topic: 'misc' };
    return {
      ...article,
      totalScore: score.relevance + score.quality + score.timeliness,
      breakdown: score,
    };
  });

  let topArticles: RankedArticle[];
  if (checkpoint.state.top.length > 0) {
    topArticles = checkpoint.state.top.map(deserializeRanked);
    console.log(`[digest] Top ${topArticles.length} restored from checkpoint`);
  } else {
    const { selected, skippedByDiversity } = selectDiverseTopArticles(scoredArticles, topN);
    topArticles = selected;
    await checkpoint.setTop(topArticles);
    console.log(
      `[digest] Top ${topN} selected with topic diversity (max 2/topic, skipped ${skippedByDiversity} similar)`
    );
  }
  console.log(
    `[digest] Score range: ${topArticles[topArticles.length - 1]?.totalScore || 0} - ${topArticles[0]?.totalScore || 0}`
  );

  // --- Stage: summarize ---
  let summaries: Map<number, { titleZh: string; summary: string; reason: string }>;
  if (checkpoint.state.summarizeComplete) {
    summaries = new Map(
      Object.entries(checkpoint.state.summaries).map(([k, v]) => [Number(k), v])
    );
    console.log(`[digest] Step 3/5: Summaries cached (${summaries.size}, skip AI)`);
  } else {
    await checkpoint.setStage('summarize');
    console.log(`[digest] Step 3/5: Generating AI summaries...`);
    const indexedTopArticles = topArticles.map((a, i) => ({ ...a, index: i }));
    summaries = await summarizeArticles(
      indexedTopArticles, aiClient, lang, aiConcurrency, summaryBatch, checkpoint
    );
  }

  const finalArticles: ScoredArticle[] = topArticles.map((a, i) => {
    const sm = summaries.get(i) || { titleZh: a.title, summary: a.description.slice(0, 200), reason: '' };
    return {
      title: a.title,
      link: a.link,
      pubDate: a.pubDate,
      description: a.description,
      sourceName: a.sourceName,
      sourceUrl: a.sourceUrl,
      score: a.totalScore,
      scoreBreakdown: {
        relevance: a.breakdown.relevance,
        quality: a.breakdown.quality,
        timeliness: a.breakdown.timeliness,
      },
      category: a.breakdown.category,
      keywords: a.breakdown.keywords,
      titleZh: sm.titleZh,
      summary: sm.summary,
      reason: sm.reason,
    };
  });

  // --- Stage: highlights ---
  let highlights: string;
  if (checkpoint.state.highlightsComplete) {
    highlights = checkpoint.state.highlights;
    console.log(`[digest] Step 4/5: Highlights cached (skip AI)`);
  } else {
    await checkpoint.setStage('highlights');
    console.log(`[digest] Step 4/5: Generating today's highlights...`);
    highlights = await generateHighlights(finalArticles, aiClient, lang);
    await checkpoint.setHighlights(highlights);
  }

  if (!checkpoint.state.reportComplete) {
    const report = generateDigestReport(finalArticles, highlights, {
      totalFeeds: rssFeeds.length,
      successFeeds: successCount,
      totalArticles: recentArticles.length,
      filteredArticles: recentArticles.length,
      hours,
    });
    await mkdir(dirname(outputPath), { recursive: true });
    await writeFile(outputPath, report);
    await checkpoint.markReportComplete();
    console.log(`[digest] 📁 Report written: ${outputPath}`);
  } else {
    console.log(`[digest] 📁 Report already written (skip)`);
  }

  // --- Stage: clippings ---
  if (!checkpoint.state.clippingsComplete) {
    await checkpoint.setStage('clippings');
    console.log(`[digest] Step 5/5: Saving full article clippings (${finalArticles.length} articles)...`);
    const translateBackends: TranslateBackend[] = [];
    if (localApiKey && localApiBase) {
      translateBackends.push({
        name: `本地 Qwen (${localApiBase}, model=${localModel || 'qwen'})`,
        apiKey: localApiKey,
        apiBase: localApiBase,
        model: localModel || 'qwen3.6-27b',
      });
    }
    if (cloudApiKey && cloudApiBase && cloudApiBase !== localApiBase) {
      translateBackends.push({
        name: `云端 Qwen (${cloudApiBase}, model=${cloudModel || 'qwen'})`,
        apiKey: cloudApiKey,
        apiBase: cloudApiBase,
        model: cloudModel || 'qwen-plus',
        cloud: true,
      });
    }
    const translateClient = createClippingTranslateClient(translateBackends, aiClient);
    const clippingCount = await saveArticleClippings(
      finalArticles,
      clippingsDir,
      clippingsConcurrency,
      translateClient,
      {
        skipLinks: new Set(checkpoint.state.clippingsDoneLinks),
        onSaved: link => checkpoint.markClippingDone(link),
        translateBatch: profile.translateBatch,
        translateConcurrency: profile.translateConcurrency,
      }
    );
    checkpoint.state.clippingsComplete = true;
    await checkpoint.flush();
    console.log(`[digest] 📎 Clippings: ${clippingCount}/${finalArticles.length} saved to ${clippingsDir}`);
  } else {
    console.log(`[digest] Step 5/5: Clippings already done (skip)`);
  }

  await checkpoint.markDoneAndClear();

  console.log('');
  console.log(`[digest] ✅ Done!`);
  console.log(`[digest] 📁 Report: ${outputPath}`);
  console.log(`[digest] 📊 Stats: ${successCount} feeds with recent → ${recentArticles.length} articles → ${finalArticles.length} selected`);

  if (finalArticles.length > 0) {
    console.log('');
    console.log(`[digest] 🏆 Top 10 Preview:`);
    for (let i = 0; i < Math.min(10, finalArticles.length); i++) {
      const a = finalArticles[i]!;
      console.log(`  ${i + 1}. ${a.titleZh || a.title}`);
      console.log(`     ${a.summary.slice(0, 80)}...`);
    }
  }
}

await main().catch((err) => {
  console.error(`[digest] Fatal error: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});

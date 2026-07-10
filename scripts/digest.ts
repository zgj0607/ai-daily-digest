import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import process from 'node:process';
import { Readability } from '@mozilla/readability';
import { parseHTML } from 'linkedom';
import TurndownService from 'turndown';

// ============================================================================
// Constants
// ============================================================================

const GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent';
const OPENAI_DEFAULT_API_BASE = 'https://api.openai.com/v1';
const OPENAI_DEFAULT_MODEL = 'gpt-4o-mini';
const FEED_FETCH_TIMEOUT_MS = 15_000;
const ARTICLE_FETCH_TIMEOUT_MS = 20_000;
const DEFAULT_FEED_CONCURRENCY = 50;
const GEMINI_BATCH_SIZE = 15;
const DEFAULT_AI_CONCURRENCY = 6;
const DEFAULT_CLIPPINGS_CONCURRENCY = 2;
const DEFAULT_CLIPPINGS_DIR = '/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings';

const RSS_FEEDS_FILE = new URL('../rss.txt', import.meta.url);
const CLIPPING_TRANSLATE_BATCH_SIZE = 6;

// ============================================================================
// Types
// ============================================================================

type CategoryId = 'ai-ml' | 'security' | 'engineering' | 'tools' | 'opinion' | 'other';
interface FeedSource {
  name: string;
  xmlUrl: string;
  htmlUrl: string;
}

const CATEGORY_META: Record<CategoryId, { emoji: string; label: string }> = {
  'ai-ml':       { emoji: '🤖', label: 'AI / ML' },
  'security':    { emoji: '🔒', label: '安全' },
  'engineering': { emoji: '⚙️', label: '工程' },
  'tools':       { emoji: '🛠', label: '工具 / 开源' },
  'opinion':     { emoji: '💡', label: '观点 / 杂谈' },
  'other':       { emoji: '📝', label: '其他' },
};

interface Article {
  title: string;
  link: string;
  pubDate: Date;
  description: string;
  sourceName: string;
  sourceUrl: string;
}

interface ScoredArticle extends Article {
  score: number;
  scoreBreakdown: {
    relevance: number;
    quality: number;
    timeliness: number;
  };
  category: CategoryId;
  keywords: string[];
  titleZh: string;
  summary: string;
  reason: string;
}

interface GeminiScoringResult {
  results: Array<{
    index: number;
    relevance: number;
    quality: number;
    timeliness: number;
    category: string;
    keywords: string[];
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

interface AIClient {
  call(prompt: string): Promise<string>;
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

function parseRSSItems(xml: string): Array<{ title: string; link: string; pubDate: string; description: string }> {
  const items: Array<{ title: string; link: string; pubDate: string; description: string }> = [];
  
  // Detect format: Atom vs RSS
  const isAtom = xml.includes('<feed') && xml.includes('xmlns="http://www.w3.org/2005/Atom"') || xml.includes('<feed ');
  
  if (isAtom) {
    // Atom format: <entry>
    const entryPattern = /<entry[\s>]([\s\S]*?)<\/entry>/gi;
    let entryMatch;
    while ((entryMatch = entryPattern.exec(xml)) !== null) {
      const entryXml = entryMatch[1];
      const title = stripHtml(getTagContent(entryXml, 'title'));
      
      // Atom link: <link href="..." rel="alternate"/>
      let link = getAttrValue(entryXml, 'link[^>]*rel="alternate"', 'href');
      if (!link) {
        link = getAttrValue(entryXml, 'link', 'href');
      }
      
      const pubDate = getTagContent(entryXml, 'published') 
        || getTagContent(entryXml, 'updated');
      
      const description = stripHtml(
        getTagContent(entryXml, 'summary') 
        || getTagContent(entryXml, 'content')
      );
      
      if (title || link) {
        items.push({ title, link, pubDate, description: description.slice(0, 500) });
      }
    }
  } else {
    // RSS format: <item>
    const itemPattern = /<item[\s>]([\s\S]*?)<\/item>/gi;
    let itemMatch;
    while ((itemMatch = itemPattern.exec(xml)) !== null) {
      const itemXml = itemMatch[1];
      const title = stripHtml(getTagContent(itemXml, 'title'));
      const link = getTagContent(itemXml, 'link') || getTagContent(itemXml, 'guid');
      const pubDate = getTagContent(itemXml, 'pubDate') 
        || getTagContent(itemXml, 'dc:date')
        || getTagContent(itemXml, 'date');
      const description = stripHtml(
        getTagContent(itemXml, 'description') 
        || getTagContent(itemXml, 'content:encoded')
      );
      
      if (title || link) {
        items.push({ title, link, pubDate, description: description.slice(0, 500) });
      }
    }
  }
  
  return items;
}

// ============================================================================
// Feed Fetching
// ============================================================================

async function fetchFeed(feed: FeedSource): Promise<Article[]> {
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
    const items = parseRSSItems(xml);
    
    return items.map(item => ({
      title: item.title,
      link: item.link,
      pubDate: parseDate(item.pubDate) || new Date(0),
      description: item.description,
      sourceName: feed.name,
      sourceUrl: feed.htmlUrl,
    }));
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    // Only log non-abort errors to reduce noise
    if (!msg.includes('abort')) {
      console.warn(`[digest] ✗ ${feed.name}: ${msg}`);
    } else {
      console.warn(`[digest] ✗ ${feed.name}: timeout`);
    }
    return [];
  }
}

function parsePositiveInt(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value || '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

async function runWithConcurrency(
  total: number,
  concurrency: number,
  workerFn: (index: number) => Promise<void>
): Promise<void> {
  if (total <= 0) {
    return;
  }

  let nextIndex = 0;
  const workerCount = Math.min(concurrency, total);

  async function worker(): Promise<void> {
    while (true) {
      const currentIndex = nextIndex++;
      if (currentIndex >= total) {
        return;
      }

      await workerFn(currentIndex);
    }
  }

  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

async function fetchAllFeeds(feeds: FeedSource[], concurrency: number): Promise<Article[]> {
  const allArticles: Article[] = [];
  let successCount = 0;
  let failCount = 0;

  if (feeds.length === 0) {
    return allArticles;
  }

  let completedCount = 0;
  let nextIndex = 0;
  const workerCount = Math.min(concurrency, feeds.length);
  const progressInterval = Math.max(10, Math.ceil(feeds.length / 20));

  async function worker(): Promise<void> {
    while (true) {
      const currentIndex = nextIndex++;
      if (currentIndex >= feeds.length) {
        return;
      }

      const articles = await fetchFeed(feeds[currentIndex]!);
      if (articles.length > 0) {
        allArticles.push(...articles);
        successCount++;
      } else {
        failCount++;
      }

      completedCount++;
      if (completedCount % progressInterval === 0 || completedCount === feeds.length) {
        console.log(`[digest] Progress: ${completedCount}/${feeds.length} feeds processed (${successCount} ok, ${failCount} failed)`);
      }
    }
  }

  await Promise.all(Array.from({ length: workerCount }, () => worker()));

  console.log(`[digest] Fetched ${allArticles.length} articles from ${successCount} feeds (${failCount} failed)`);
  return allArticles;
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
    }),
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => 'Unknown error');
    throw new Error(`OpenAI-compatible API error (${response.status}): ${errorText}`);
  }

  const data = await response.json() as {
    choices?: Array<{
      message?: {
        content?: string | Array<{ type?: string; text?: string }>;
      };
    }>;
  };

  const content = data.choices?.[0]?.message?.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter(item => item.type === 'text' && typeof item.text === 'string')
      .map(item => item.text)
      .join('\n');
  }
  return '';
}

function inferOpenAIModel(apiBase: string): string {
  const base = apiBase.toLowerCase();
  if (base.includes('deepseek')) return 'deepseek-chat';
  return OPENAI_DEFAULT_MODEL;
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

function parseJsonResponse<T>(text: string): T {
  let jsonText = text.trim();
  // Strip markdown code blocks if present
  if (jsonText.startsWith('```')) {
    jsonText = jsonText.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '');
  }
  return JSON.parse(jsonText) as T;
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
      "keywords": ["Rust", "compiler", "performance"]
    }
  ]
}`;
}

async function scoreArticlesWithAI(
  articles: Article[],
  aiClient: AIClient,
  aiConcurrency: number
): Promise<Map<number, { relevance: number; quality: number; timeliness: number; category: CategoryId; keywords: string[] }>> {
  const allScores = new Map<number, { relevance: number; quality: number; timeliness: number; category: CategoryId; keywords: string[] }>();
  
  const indexed = articles.map((article, index) => ({
    index,
    title: article.title,
    description: article.description,
    sourceName: article.sourceName,
  }));
  
  const batches: typeof indexed[] = [];
  for (let i = 0; i < indexed.length; i += GEMINI_BATCH_SIZE) {
    batches.push(indexed.slice(i, i + GEMINI_BATCH_SIZE));
  }
  
  console.log(`[digest] AI scoring: ${articles.length} articles in ${batches.length} batches`);
  
  const validCategories = new Set<string>(['ai-ml', 'security', 'engineering', 'tools', 'opinion', 'other']);

  let completedBatches = 0;
  await runWithConcurrency(batches.length, aiConcurrency, async (batchIndex) => {
    const batch = batches[batchIndex]!;
    try {
      const prompt = buildScoringPrompt(batch);
      const responseText = await aiClient.call(prompt);
      const parsed = parseJsonResponse<GeminiScoringResult>(responseText);
      
      if (parsed.results && Array.isArray(parsed.results)) {
        for (const result of parsed.results) {
          const clamp = (v: number) => Math.min(10, Math.max(1, Math.round(v)));
          const cat = (validCategories.has(result.category) ? result.category : 'other') as CategoryId;
          allScores.set(result.index, {
            relevance: clamp(result.relevance),
            quality: clamp(result.quality),
            timeliness: clamp(result.timeliness),
            category: cat,
            keywords: Array.isArray(result.keywords) ? result.keywords.slice(0, 4) : [],
          });
        }
      }
    } catch (error) {
      console.warn(`[digest] Scoring batch failed: ${error instanceof Error ? error.message : String(error)}`);
      for (const item of batch) {
        allScores.set(item.index, { relevance: 5, quality: 5, timeliness: 5, category: 'other', keywords: [] });
      }
    } finally {
      completedBatches++;
      console.log(`[digest] Scoring progress: ${completedBatches}/${batches.length} batches`);
    }
  });
  
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
  aiConcurrency: number
): Promise<Map<number, { titleZh: string; summary: string; reason: string }>> {
  const summaries = new Map<number, { titleZh: string; summary: string; reason: string }>();
  
  const indexed = articles.map(a => ({
    index: a.index,
    title: a.title,
    description: a.description,
    sourceName: a.sourceName,
    link: a.link,
  }));
  
  const batches: typeof indexed[] = [];
  for (let i = 0; i < indexed.length; i += GEMINI_BATCH_SIZE) {
    batches.push(indexed.slice(i, i + GEMINI_BATCH_SIZE));
  }
  
  console.log(`[digest] Generating summaries for ${articles.length} articles in ${batches.length} batches`);

  let completedBatches = 0;
  await runWithConcurrency(batches.length, aiConcurrency, async (batchIndex) => {
    const batch = batches[batchIndex]!;
    try {
      const prompt = buildSummaryPrompt(batch, lang);
      const responseText = await aiClient.call(prompt);
      const parsed = parseJsonResponse<GeminiSummaryResult>(responseText);
      
      if (parsed.results && Array.isArray(parsed.results)) {
        for (const result of parsed.results) {
          summaries.set(result.index, {
            titleZh: result.titleZh || '',
            summary: result.summary || '',
            reason: result.reason || '',
          });
        }
      }
    } catch (error) {
      console.warn(`[digest] Summary batch failed: ${error instanceof Error ? error.message : String(error)}`);
      for (const item of batch) {
        summaries.set(item.index, { titleZh: item.title, summary: item.title, reason: '' });
      }
    } finally {
      completedBatches++;
      console.log(`[digest] Summary progress: ${completedBatches}/${batches.length} batches`);
    }
  });
  
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
// Visualization Helpers
// ============================================================================

function humanizeTime(pubDate: Date): string {
  const diffMs = Date.now() - pubDate.getTime();
  const diffMins = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMs / 3_600_000);
  const diffDays = Math.floor(diffMs / 86_400_000);

  if (diffMins < 60) return `${diffMins} 分钟前`;
  if (diffHours < 24) return `${diffHours} 小时前`;
  if (diffDays < 7) return `${diffDays} 天前`;
  return pubDate.toISOString().slice(0, 10);
}

function generateKeywordBarChart(articles: ScoredArticle[]): string {
  const kwCount = new Map<string, number>();
  for (const a of articles) {
    for (const kw of a.keywords) {
      const normalized = kw.toLowerCase();
      kwCount.set(normalized, (kwCount.get(normalized) || 0) + 1);
    }
  }

  const sorted = Array.from(kwCount.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12);

  if (sorted.length === 0) return '';

  const labels = sorted.map(([k]) => `"${k}"`).join(', ');
  const values = sorted.map(([, v]) => v).join(', ');
  const maxVal = sorted[0][1];

  let chart = '```mermaid\n';
  chart += `xychart-beta horizontal\n`;
  chart += `    title "高频关键词"\n`;
  chart += `    x-axis [${labels}]\n`;
  chart += `    y-axis "出现次数" 0 --> ${maxVal + 2}\n`;
  chart += `    bar [${values}]\n`;
  chart += '```\n';

  return chart;
}

function generateCategoryPieChart(articles: ScoredArticle[]): string {
  const catCount = new Map<CategoryId, number>();
  for (const a of articles) {
    catCount.set(a.category, (catCount.get(a.category) || 0) + 1);
  }

  if (catCount.size === 0) return '';

  const sorted = Array.from(catCount.entries()).sort((a, b) => b[1] - a[1]);

  let chart = '```mermaid\n';
  chart += `pie showData\n`;
  chart += `    title "文章分类分布"\n`;
  for (const [cat, count] of sorted) {
    const meta = CATEGORY_META[cat];
    chart += `    "${meta.emoji} ${meta.label}" : ${count}\n`;
  }
  chart += '```\n';

  return chart;
}

function generateAsciiBarChart(articles: ScoredArticle[]): string {
  const kwCount = new Map<string, number>();
  for (const a of articles) {
    for (const kw of a.keywords) {
      const normalized = kw.toLowerCase();
      kwCount.set(normalized, (kwCount.get(normalized) || 0) + 1);
    }
  }

  const sorted = Array.from(kwCount.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10);

  if (sorted.length === 0) return '';

  const maxVal = sorted[0][1];
  const maxBarWidth = 20;
  const maxLabelLen = Math.max(...sorted.map(([k]) => k.length));

  let chart = '```\n';
  for (const [label, value] of sorted) {
    const barLen = Math.max(1, Math.round((value / maxVal) * maxBarWidth));
    const bar = '█'.repeat(barLen) + '░'.repeat(maxBarWidth - barLen);
    chart += `${label.padEnd(maxLabelLen)} │ ${bar} ${value}\n`;
  }
  chart += '```\n';

  return chart;
}

function generateTagCloud(articles: ScoredArticle[]): string {
  const kwCount = new Map<string, number>();
  for (const a of articles) {
    for (const kw of a.keywords) {
      const normalized = kw.toLowerCase();
      kwCount.set(normalized, (kwCount.get(normalized) || 0) + 1);
    }
  }

  const sorted = Array.from(kwCount.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 20);

  if (sorted.length === 0) return '';

  return sorted
    .map(([word, count], i) => i < 3 ? `**${word}**(${count})` : `${word}(${count})`)
    .join(' · ');
}

// ============================================================================
// Report Generation
// ============================================================================

function generateDigestReport(articles: ScoredArticle[], highlights: string, stats: {
  totalFeeds: number;
  successFeeds: number;
  totalArticles: number;
  filteredArticles: number;
  hours: number;
  lang: string;
}): string {
  const now = new Date();
  const dateStr = now.toISOString().split('T')[0];
  
  let report = `# 📰 AI 博客每日精选 — ${dateStr}\n\n`;
  report += `> 来自 Karpathy 推荐和阮一峰网络日志出现的 ${stats.totalFeeds} 个顶级技术博客，AI 精选 Top ${articles.length}\n\n`;

  // ── Today's Highlights ──
  if (highlights) {
    report += `## 📝 今日行动指南\n\n`;
    report += `${highlights}\n\n`;
    report += `---\n\n`;
  }

  // ── Top 10 Deep Showcase ──
  if (articles.length > 0) {
    report += `## 🏆 今日必读\n\n`;
    for (let i = 0; i < Math.min(10, articles.length); i++) {
      const a = articles[i];
      const medal = ['🥇', '🥈', '🥉'][i] || `${i + 1}.`;
      const catMeta = CATEGORY_META[a.category];
      
      report += `${medal} **${a.titleZh || a.title}**\n\n`;
      report += `[${a.title}](${a.link}) — ${a.sourceName} · ${humanizeTime(a.pubDate)} · ${catMeta.emoji} ${catMeta.label}\n\n`;
      report += `> ${a.summary}\n\n`;
      if (a.reason) {
        report += `💡 **为什么值得读**: ${a.reason}\n\n`;
      }
      if (a.keywords.length > 0) {
        report += `🏷️ ${a.keywords.join(', ')}\n\n`;
      }
    }
    report += `---\n\n`;
  }

  // ── Visual Statistics ──
  report += `## 📊 数据概览\n\n`;

  report += `| 扫描源 | 抓取文章 | 时间范围 | 精选 |\n`;
  report += `|:---:|:---:|:---:|:---:|\n`;
  report += `| ${stats.successFeeds}/${stats.totalFeeds} | ${stats.totalArticles} 篇 → ${stats.filteredArticles} 篇 | ${stats.hours}h | **${articles.length} 篇** |\n\n`;

  const pieChart = generateCategoryPieChart(articles);
  if (pieChart) {
    report += `### 分类分布\n\n${pieChart}\n`;
  }

  const barChart = generateKeywordBarChart(articles);
  if (barChart) {
    report += `### 高频关键词\n\n${barChart}\n`;
  }

  const asciiChart = generateAsciiBarChart(articles);
  if (asciiChart) {
    report += `<details>\n<summary>📈 纯文本关键词图（终端友好）</summary>\n\n${asciiChart}\n</details>\n\n`;
  }

  const tagCloud = generateTagCloud(articles);
  if (tagCloud) {
    report += `### 🏷️ 话题标签\n\n${tagCloud}\n\n`;
  }

  report += `---\n\n`;

  // ── Category-Grouped Articles ──
  const categoryGroups = new Map<CategoryId, ScoredArticle[]>();
  for (const a of articles) {
    const list = categoryGroups.get(a.category) || [];
    list.push(a);
    categoryGroups.set(a.category, list);
  }

  const sortedCategories = Array.from(categoryGroups.entries())
    .sort((a, b) => b[1].length - a[1].length);

  let globalIndex = 0;
  for (const [catId, catArticles] of sortedCategories) {
    const catMeta = CATEGORY_META[catId];
    report += `## ${catMeta.emoji} ${catMeta.label}\n\n`;

    for (const a of catArticles) {
      globalIndex++;
      const scoreTotal = a.scoreBreakdown.relevance + a.scoreBreakdown.quality + a.scoreBreakdown.timeliness;

      report += `### ${globalIndex}. ${a.titleZh || a.title}\n\n`;
      report += `[${a.title}](${a.link}) — **${a.sourceName}** · ${humanizeTime(a.pubDate)} · ⭐ ${scoreTotal}/30\n\n`;
      report += `> ${a.summary}\n\n`;
      if (a.keywords.length > 0) {
        report += `🏷️ ${a.keywords.join(', ')}\n\n`;
      }
      report += `---\n\n`;
    }
  }

  // ── Footer ──
  report += `*生成于 ${dateStr} ${now.toISOString().split('T')[1]?.slice(0, 5) || ''} | 扫描 ${stats.successFeeds} 源 → 获取 ${stats.totalArticles} 篇 → 精选 ${articles.length} 篇*\n`;
  report += `*基于 [Hacker News Popularity Contest 2025](https://refactoringenglish.com/tools/hn-popularity/) RSS 源列表，由 [Andrej Karpathy](https://x.com/karpathy) 推荐*\n`;
  report += `*由「懂点儿AI」制作，欢迎关注同名微信公众号获取更多 AI 实用技巧 💡*\n`;

  return report;
}

// ============================================================================
// Article Clippings (Full Markdown Export)
// ============================================================================

type MarkdownBlock = { type: 'code' | 'text'; content: string };

function formatDateCompact(date: Date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}${m}${d}`;
}

function sanitizeFilename(title: string, maxLen = 120): string {
  return title
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLen) || 'untitled';
}

function parseMarkdownBlocks(markdown: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = markdown.split('\n');
  let textBuffer: string[] = [];
  let index = 0;

  const flushText = () => {
    const content = textBuffer.join('\n').trimEnd();
    textBuffer = [];
    if (content.trim()) {
      blocks.push({ type: 'text', content });
    }
  };

  while (index < lines.length) {
    const line = lines[index]!;
    if (line.trim().startsWith('```')) {
      flushText();
      const codeLines = [line];
      index++;
      while (index < lines.length) {
        codeLines.push(lines[index]!);
        if (lines[index]!.trim().startsWith('```') && codeLines.length > 1) {
          index++;
          break;
        }
        index++;
      }
      blocks.push({ type: 'code', content: codeLines.join('\n') });
      continue;
    }

    if (line.trim() === '') {
      flushText();
      index++;
      continue;
    }

    textBuffer.push(line);
    index++;
  }

  flushText();
  return blocks;
}

function countChineseChars(text: string): number {
  return (text.match(/[\u4e00-\u9fff]/g) || []).length;
}

function isMostlyChinese(text: string): boolean {
  const meaningful = text.replace(/[#>*\-\[\]()!`~\d\s]/g, '');
  if (!meaningful) return false;
  return countChineseChars(meaningful) / meaningful.length > 0.3;
}

function shouldTranslateBlock(content: string): boolean {
  const plain = content.replace(/^#{1,6}\s+/, '').trim();
  if (!plain) return false;
  if (isMostlyChinese(plain)) return false;
  if (plain.length < 12) return false;
  if (/^!\[.*\]\(.*\)$/.test(plain)) return false;
  if (/^https?:\/\//.test(plain)) return false;
  return true;
}

async function translateTextBlocks(blocks: string[], aiClient: AIClient): Promise<string[]> {
  if (blocks.length === 0) return [];

  const prompt = `你是沉浸式翻译助手。请将以下 Markdown 段落逐条翻译成自然流畅的中文。

要求：
- 保留原文中的链接、代码片段、专有名词（可附中文说明）
- 标题段落（以 # 开头）翻译后仍保留相同数量的 # 前缀
- 列表项翻译后仍保留 - 或数字序号前缀
- 语气忠实原文，便于阅读
- 只返回 JSON，不要 markdown 代码块

输入共 ${blocks.length} 条，按 index 返回翻译：

${blocks.map((block, index) => `--- index ${index} ---\n${block}`).join('\n\n')}

返回格式：
{
  "translations": [
    { "index": 0, "text": "中文翻译" }
  ]
}`;

  try {
    const responseText = await aiClient.call(prompt);
    const parsed = parseJsonResponse<{ translations: Array<{ index: number; text: string }> }>(responseText);
    const results = new Array<string>(blocks.length).fill('');
    if (parsed.translations && Array.isArray(parsed.translations)) {
      for (const item of parsed.translations) {
        if (item.index >= 0 && item.index < blocks.length && item.text) {
          results[item.index] = item.text.trim();
        }
      }
    }
    return results;
  } catch (error) {
    console.warn(`[digest] Clipping translation batch failed: ${error instanceof Error ? error.message : String(error)}`);
    return blocks.map(() => '');
  }
}

async function addBilingualTranslation(markdown: string, aiClient: AIClient): Promise<string> {
  const blocks = parseMarkdownBlocks(markdown);
  const textBlocks = blocks.filter(block => block.type === 'text');
  const allText = textBlocks.map(block => block.content).join('\n');

  if (!allText.trim() || isMostlyChinese(allText)) {
    return markdown;
  }

  const translatable: Array<{ blockIndex: number; content: string }> = [];
  blocks.forEach((block, blockIndex) => {
    if (block.type === 'text' && shouldTranslateBlock(block.content)) {
      translatable.push({ blockIndex, content: block.content });
    }
  });

  if (translatable.length === 0) {
    return markdown;
  }

  const translations = new Map<number, string>();
  for (let start = 0; start < translatable.length; start += CLIPPING_TRANSLATE_BATCH_SIZE) {
    const batch = translatable.slice(start, start + CLIPPING_TRANSLATE_BATCH_SIZE);
    const translated = await translateTextBlocks(batch.map(item => item.content), aiClient);
    batch.forEach((item, index) => {
      const text = translated[index]?.trim();
      if (text) {
        translations.set(item.blockIndex, text);
      }
    });
  }

  const parts: string[] = [];
  blocks.forEach((block, blockIndex) => {
    parts.push(block.content);
    const translation = translations.get(blockIndex);
    if (translation) {
      parts.push('');
      parts.push(translation);
    }
  });

  return parts.join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
}

function createTurndownService(): TurndownService {
  const turndown = new TurndownService({
    headingStyle: 'atx',
    codeBlockStyle: 'fenced',
    bulletListMarker: '-',
  });

  turndown.remove(['script', 'style', 'nav', 'footer', 'aside', 'iframe', 'form', 'noscript']);
  return turndown;
}

function extractArticleMarkdownFromHtml(html: string): string | null {
  const { document } = parseHTML(html);
  const reader = new Readability(document, { charThreshold: 100 });
  const article = reader.parse();

  if (!article?.content) {
    return null;
  }

  const turndown = createTurndownService();
  const markdown = turndown.turndown(article.content).replace(/\n{3,}/g, '\n\n').trim();
  return markdown.length > 100 ? markdown : null;
}

async function fetchArticleMarkdown(url: string): Promise<string | null> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), ARTICLE_FETCH_TIMEOUT_MS);

    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        'User-Agent': 'AI-Daily-Digest/1.0 (Article Reader)',
        'Accept': 'text/html,application/xhtml+xml,text/markdown,text/plain,*/*',
      },
      redirect: 'follow',
    });

    clearTimeout(timeout);

    if (!response.ok) {
      return null;
    }

    const contentType = response.headers.get('content-type') || '';
    const body = await response.text();

    if (contentType.includes('text/markdown') || url.endsWith('.md')) {
      const markdown = body.trim();
      return markdown.length > 100 ? markdown : null;
    }

    return extractArticleMarkdownFromHtml(body);
  } catch {
    return null;
  }
}

function buildClippingMarkdown(article: ScoredArticle, content: string): string {
  const dateStr = article.pubDate.toISOString().slice(0, 10);
  const lines = [
    `# ${article.titleZh || article.title}`,
    '',
    `> 来源: [${article.sourceName}](${article.sourceUrl})`,
    `> 原文: [${article.title}](${article.link})`,
    `> 发布: ${dateStr}`,
    '',
    '---',
    '',
    content,
  ];
  return lines.join('\n');
}

async function saveArticleClippings(
  articles: ScoredArticle[],
  clippingsDir: string,
  concurrency: number,
  aiClient: AIClient
): Promise<number> {
  await mkdir(clippingsDir, { recursive: true });

  const datePrefix = formatDateCompact();
  const usedNames = new Set<string>();
  const tasks = articles.map((article) => {
    const baseName = sanitizeFilename(article.title);
    let fileName = `${datePrefix}-${baseName}.md`;

    if (usedNames.has(fileName)) {
      let suffix = 2;
      while (usedNames.has(`${datePrefix}-${baseName}-${suffix}.md`)) {
        suffix++;
      }
      fileName = `${datePrefix}-${baseName}-${suffix}.md`;
    }
    usedNames.add(fileName);

    return { article, fileName };
  });

  let savedCount = 0;

  await runWithConcurrency(tasks.length, concurrency, async (index) => {
    const { article, fileName } = tasks[index]!;
    const rawMarkdown = await fetchArticleMarkdown(article.link);
    if (!rawMarkdown) {
      console.warn(`[digest] ✗ Clipping failed: ${article.title}`);
      return;
    }

    const content = await addBilingualTranslation(rawMarkdown, aiClient);
    const filePath = `${clippingsDir}/${fileName}`;
    await writeFile(filePath, buildClippingMarkdown(article, content), 'utf8');
    savedCount++;
    console.log(`[digest] ✓ Clipping saved: ${fileName}`);
  });

  return savedCount;
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
  --ai-concurrency <n> Max concurrent AI batch requests (default: 6)
  --clippings-dir <path> Directory to save full article markdown (default: ~/my-kb/raw/clippings)
  --clippings-concurrency <n> Max concurrent article fetches for clippings (default: 4)
  --output <path> Output file path (default: ./digest-YYYYMMDD.md)
  --help          Show this help

Environment:
  GEMINI_API_KEY   Optional but recommended. Get one at https://aistudio.google.com/apikey
  OPENAI_API_KEY   Optional fallback key for OpenAI-compatible APIs
  OPENAI_API_BASE  Optional fallback base URL (default: https://api.openai.com/v1)
  OPENAI_MODEL     Optional fallback model (default: deepseek-chat for DeepSeek base, else gpt-4o-mini)
  FEED_CONCURRENCY Optional RSS fetch concurrency override
  AI_CONCURRENCY   Optional AI batch concurrency override

Examples:
  bun scripts/digest.ts --hours 24 --top-n 30 --lang zh
  bun scripts/digest.ts --feed-concurrency 80 --hours 24 --top-n 30 --lang zh
  bun scripts/digest.ts --ai-concurrency 12 --hours 24 --top-n 30 --lang zh
  bun scripts/digest.ts --hours 72 --top-n 30 --lang en --output ./my-digest.md
`);
  process.exit(0);
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes('--help') || args.includes('-h')) printUsage();
  
  let hours = 48;
  let topN = 30;
  let lang: 'zh' | 'en' = 'zh';
  let outputPath = '';
  let clippingsDir = process.env.CLIPPINGS_DIR?.trim() || DEFAULT_CLIPPINGS_DIR;
  let clippingsConcurrency = parsePositiveInt(process.env.CLIPPINGS_CONCURRENCY, DEFAULT_CLIPPINGS_CONCURRENCY);
  let feedConcurrency = parsePositiveInt(process.env.FEED_CONCURRENCY, DEFAULT_FEED_CONCURRENCY);
  let aiConcurrency = parsePositiveInt(process.env.AI_CONCURRENCY, DEFAULT_AI_CONCURRENCY);
  
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
      aiConcurrency = parsePositiveInt(args[++i], aiConcurrency);
    } else if (arg === '--clippings-dir' && args[i + 1]) {
      clippingsDir = args[++i]!;
    } else if (arg === '--clippings-concurrency' && args[i + 1]) {
      clippingsConcurrency = parsePositiveInt(args[++i], clippingsConcurrency);
    } else if (arg === '--output' && args[i + 1]) {
      outputPath = args[++i]!;
    }
  }
  
  const geminiApiKey = process.env.GEMINI_API_KEY;
  const openaiApiKey = process.env.OPENAI_API_KEY;
  const openaiApiBase = process.env.OPENAI_API_BASE;
  const openaiModel = process.env.OPENAI_MODEL;

  if (!geminiApiKey && !openaiApiKey) {
    console.error('[digest] Error: Missing API key. Set GEMINI_API_KEY and/or OPENAI_API_KEY.');
    console.error('[digest] Gemini key: https://aistudio.google.com/apikey');
    process.exit(1);
  }

  const aiClient = createAIClient({
    geminiApiKey,
    openaiApiKey,
    openaiApiBase,
    openaiModel,
  });
  
  if (!outputPath) {
    const dateStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    outputPath = `./digest-${dateStr}.md`;
  }
  
  console.log(`[digest] === AI Daily Digest ===`);
  console.log(`[digest] Time range: ${hours} hours`);
  console.log(`[digest] Top N: ${topN}`);
  console.log(`[digest] Language: ${lang}`);
  console.log(`[digest] Feed concurrency: ${feedConcurrency}`);
  console.log(`[digest] AI concurrency: ${aiConcurrency}`);
  console.log(`[digest] Clippings dir: ${clippingsDir}`);
  console.log(`[digest] Output: ${outputPath}`);
  console.log(`[digest] AI provider: ${geminiApiKey ? 'Gemini (primary)' : 'OpenAI-compatible (primary)'}`);
  if (openaiApiKey) {
    const resolvedBase = (openaiApiBase?.trim() || OPENAI_DEFAULT_API_BASE).replace(/\/+$/, '');
    const resolvedModel = openaiModel?.trim() || inferOpenAIModel(resolvedBase);
    console.log(`[digest] Fallback: ${resolvedBase} (model=${resolvedModel})`);
  }
  console.log('');
  
  const rssFeeds = await loadRSSFeeds();
  console.log(`[digest] RSS sources loaded from rss.txt: ${rssFeeds.length}`);
  console.log(`[digest] Step 1/6: Fetching ${rssFeeds.length} RSS feeds...`);
  const allArticles = await fetchAllFeeds(rssFeeds, feedConcurrency);
  
  if (allArticles.length === 0) {
    console.error('[digest] Error: No articles fetched from any feed. Check network connection.');
    process.exit(1);
  }
  
  console.log(`[digest] Step 2/6: Filtering by time range (${hours} hours)...`);
  const cutoffTime = new Date(Date.now() - hours * 60 * 60 * 1000);
  const recentArticles = allArticles.filter(a => a.pubDate.getTime() > cutoffTime.getTime());
  
  console.log(`[digest] Found ${recentArticles.length} articles within last ${hours} hours`);
  
  if (recentArticles.length === 0) {
    console.error(`[digest] Error: No articles found within the last ${hours} hours.`);
    console.error(`[digest] Try increasing --hours (e.g., --hours 168 for one week)`);
    process.exit(1);
  }
  
  console.log(`[digest] Step 3/6: AI scoring ${recentArticles.length} articles...`);
  const scores = await scoreArticlesWithAI(recentArticles, aiClient, aiConcurrency);
  
  const scoredArticles = recentArticles.map((article, index) => {
    const score = scores.get(index) || { relevance: 5, quality: 5, timeliness: 5, category: 'other' as CategoryId, keywords: [] };
    return {
      ...article,
      totalScore: score.relevance + score.quality + score.timeliness,
      breakdown: score,
    };
  });
  
  scoredArticles.sort((a, b) => b.totalScore - a.totalScore);
  const topArticles = scoredArticles.slice(0, topN);
  
  console.log(`[digest] Top ${topN} articles selected (score range: ${topArticles[topArticles.length - 1]?.totalScore || 0} - ${topArticles[0]?.totalScore || 0})`);
  
  console.log(`[digest] Step 4/6: Generating AI summaries...`);
  const indexedTopArticles = topArticles.map((a, i) => ({ ...a, index: i }));
  const summaries = await summarizeArticles(indexedTopArticles, aiClient, lang, aiConcurrency);
  
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
  
  console.log(`[digest] Step 5/6: Generating today's highlights...`);
  const highlights = await generateHighlights(finalArticles, aiClient, lang);

  console.log(`[digest] Step 6/6: Saving full article clippings (${finalArticles.length} articles)...`);
  const clippingCount = await saveArticleClippings(finalArticles, clippingsDir, clippingsConcurrency, aiClient);
  
  const successfulSources = new Set(allArticles.map(a => a.sourceName));
  
  const report = generateDigestReport(finalArticles, highlights, {
    totalFeeds: rssFeeds.length,
    successFeeds: successfulSources.size,
    totalArticles: allArticles.length,
    filteredArticles: recentArticles.length,
    hours,
    lang,
  });
  
  await mkdir(dirname(outputPath), { recursive: true });
  await writeFile(outputPath, report);
  
  console.log('');
  console.log(`[digest] ✅ Done!`);
  console.log(`[digest] 📁 Report: ${outputPath}`);
  console.log(`[digest] 📎 Clippings: ${clippingCount}/${finalArticles.length} saved to ${clippingsDir}`);
  console.log(`[digest] 📊 Stats: ${successfulSources.size} sources → ${allArticles.length} articles → ${recentArticles.length} recent → ${finalArticles.length} selected`);
  
  if (finalArticles.length > 0) {
    console.log('');
    console.log(`[digest] 🏆 Top 10 Preview:`);
    for (let i = 0; i < Math.min(10, finalArticles.length); i++) {
      const a = finalArticles[i];
      console.log(`  ${i + 1}. ${a.titleZh || a.title}`);
      console.log(`     ${a.summary.slice(0, 80)}...`);
    }
  }
}

await main().catch((err) => {
  console.error(`[digest] Fatal error: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});

import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import process from 'node:process';
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

// ============================================================================
// Constants
// ============================================================================

const GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent';
const OPENAI_DEFAULT_API_BASE = 'https://api.openai.com/v1';
const OPENAI_DEFAULT_MODEL = 'gpt-4o-mini';
const FEED_FETCH_TIMEOUT_MS = 15_000;
const DEFAULT_FEED_CONCURRENCY = 50;
const GEMINI_BATCH_SIZE = 15;
const DEFAULT_AI_CONCURRENCY = 6;
const DEFAULT_CLIPPINGS_CONCURRENCY = 2;
const DEFAULT_CLIPPINGS_DIR = '/Users/zhou/Documents/PycharmProject/my-kb/raw/clippings';

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
  aiConcurrency: number
): Promise<Map<number, ArticleScoreBreakdown>> {
  const allScores = new Map<number, ArticleScoreBreakdown>();
  
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
            topic: normalizeTopicSlug(result.topic),
          });
        }
      }
    } catch (error) {
      console.warn(`[digest] Scoring batch failed: ${error instanceof Error ? error.message : String(error)}`);
      for (const item of batch) {
        allScores.set(item.index, {
          relevance: 5, quality: 5, timeliness: 5, category: 'other', keywords: [], topic: 'misc',
        });
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
  --clippings-concurrency <n> Max concurrent article fetches for clippings (default: 2)
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
  
  const scoredArticles: RankedArticle[] = recentArticles.map((article, index) => {
    const score = scores.get(index) || {
      relevance: 5, quality: 5, timeliness: 5, category: 'other' as CategoryId, keywords: [], topic: 'misc',
    };
    return {
      ...article,
      totalScore: score.relevance + score.quality + score.timeliness,
      breakdown: score,
    };
  });

  const { selected: topArticles, skippedByDiversity } = selectDiverseTopArticles(scoredArticles, topN);

  console.log(
    `[digest] Top ${topN} selected with topic diversity (max 2/topic, skipped ${skippedByDiversity} similar)`
  );
  console.log(
    `[digest] Score range: ${topArticles[topArticles.length - 1]?.totalScore || 0} - ${topArticles[0]?.totalScore || 0}`
  );
  
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

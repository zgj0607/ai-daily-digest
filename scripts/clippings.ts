import { writeFile, mkdir } from 'node:fs/promises';
import { Readability } from '@mozilla/readability';
import { parseHTML } from 'linkedom';
import TurndownService from 'turndown';
import type { AIClient, ScoredArticle } from './types.ts';
import { parseJsonResponse, runWithConcurrency } from './utils.ts';

const ARTICLE_FETCH_TIMEOUT_MS = 20_000;
const TRANSLATE_BATCH = 6;

type Block = { type: 'code' | 'text'; content: string };

const turndown = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-' });
turndown.remove(['script', 'style', 'nav', 'footer', 'aside', 'iframe', 'form', 'noscript']);

function formatDateCompact(date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}${m}${d}`;
}

function sanitizeFilename(title: string): string {
  return title.replace(/[<>:"/\\|?*\x00-\x1f]/g, '').replace(/\s+/g, ' ').trim().slice(0, 120) || 'untitled';
}

function parseBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  const lines = markdown.split('\n');
  let buf: string[] = [];
  let i = 0;
  const flush = () => {
    const content = buf.join('\n').trimEnd();
    buf = [];
    if (content.trim()) blocks.push({ type: 'text', content });
  };
  while (i < lines.length) {
    const line = lines[i]!;
    if (line.trim().startsWith('```')) {
      flush();
      const code = [line];
      i++;
      while (i < lines.length) {
        code.push(lines[i]!);
        if (lines[i]!.trim().startsWith('```') && code.length > 1) { i++; break; }
        i++;
      }
      blocks.push({ type: 'code', content: code.join('\n') });
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    buf.push(line);
    i++;
  }
  flush();
  return blocks;
}

function stripForLang(text: string): string {
  return text.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/`[^`]+`/g, '')
    .replace(/^#{1,6}\s+/gm, '').replace(/^[-*+]\s+/gm, '').replace(/^\d+\.\s+/gm, '')
    .replace(/[#>*_\[\]()!~`|]/g, '').replace(/\s+/g, '').trim();
}

function countChinese(text: string): number {
  return (text.match(/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/g) || []).length;
}

function countLetters(text: string): number {
  return (text.match(/[a-zA-Z\u0400-\u04ff\u00C0-\u024F\u1E00-\u1EFF\u0370-\u03ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u3040-\u30ff\uac00-\ud7af]/g) || []).length;
}

function isChinese(text: string, ratio = 0.2): boolean {
  const plain = stripForLang(text);
  if (!plain) return false;
  const cn = countChinese(plain);
  if (!cn) return false;
  const letters = cn + countLetters(plain);
  return letters > 0 && cn / letters >= ratio;
}

function shouldTranslate(content: string): boolean {
  const plain = content.replace(/^#{1,6}\s+/, '').trim();
  return plain.length >= 8 && !isChinese(content) && !/^!\[.*\]\(.*\)$/.test(plain) && !/^https?:\/\//.test(plain) && countLetters(plain) >= 4;
}

async function translateBlocks(blocks: string[], aiClient: AIClient): Promise<string[]> {
  if (!blocks.length) return [];
  const prompt = `你是沉浸式翻译助手。将以下非中文 Markdown 段落逐条译为中文。已是中文的返回空字符串 ""。保留链接和格式。只返回 JSON：\n{"translations":[{"index":0,"text":"..."}]}\n\n${blocks.map((b, i) => `--- ${i} ---\n${b}`).join('\n\n')}`;
  try {
    const parsed = parseJsonResponse<{ translations: Array<{ index: number; text: string }> }>(await aiClient.call(prompt));
    const out = new Array<string>(blocks.length).fill('');
    for (const item of parsed.translations || []) {
      if (item.index >= 0 && item.index < blocks.length && item.text) out[item.index] = item.text.trim();
    }
    return out;
  } catch {
    return blocks.map(() => '');
  }
}

async function addBilingualTranslation(markdown: string, aiClient: AIClient): Promise<string> {
  const blocks = parseBlocks(markdown);
  const allText = blocks.filter(b => b.type === 'text').map(b => b.content).join('\n');
  if (!allText.trim() || isChinese(allText, 0.35)) return markdown;

  const jobs: Array<{ idx: number; content: string }> = [];
  blocks.forEach((block, idx) => { if (block.type === 'text' && shouldTranslate(block.content)) jobs.push({ idx, content: block.content }); });
  if (!jobs.length) return markdown;

  const translations = new Map<number, string>();
  for (let s = 0; s < jobs.length; s += TRANSLATE_BATCH) {
    const batch = jobs.slice(s, s + TRANSLATE_BATCH);
    const translated = await translateBlocks(batch.map(j => j.content), aiClient);
    batch.forEach((job, i) => {
      const text = translated[i]?.trim();
      if (text && text !== job.content.trim()) translations.set(job.idx, text);
    });
  }

  return blocks.flatMap((block, idx) => {
    const zh = translations.get(idx);
    return zh ? [block.content, '', zh] : [block.content];
  }).join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
}

function extractMarkdownFromHtml(html: string): string | null {
  const article = new Readability(parseHTML(html).document, { charThreshold: 100 }).parse();
  if (!article?.content) return null;
  const md = turndown.turndown(article.content).replace(/\n{3,}/g, '\n\n').trim();
  return md.length > 100 ? md : null;
}

async function fetchArticleMarkdown(url: string): Promise<string | null> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), ARTICLE_FETCH_TIMEOUT_MS);
    const response = await fetch(url, {
      signal: controller.signal,
      headers: { 'User-Agent': 'AI-Daily-Digest/1.0', Accept: 'text/html,application/xhtml+xml,text/markdown,text/plain,*/*' },
      redirect: 'follow',
    });
    clearTimeout(timeout);
    if (!response.ok) return null;
    const body = await response.text();
    if (response.headers.get('content-type')?.includes('text/markdown') || url.endsWith('.md')) {
      const md = body.trim();
      return md.length > 100 ? md : null;
    }
    return extractMarkdownFromHtml(body);
  } catch {
    return null;
  }
}

function buildClippingMarkdown(article: ScoredArticle, content: string): string {
  return [
    `# ${article.titleZh || article.title}`, '',
    `> 来源: [${article.sourceName}](${article.sourceUrl})`,
    `> 原文: [${article.title}](${article.link})`,
    `> 发布: ${article.pubDate.toISOString().slice(0, 10)}`, '',
    '---', '', content,
  ].join('\n');
}

export async function saveArticleClippings(
  articles: ScoredArticle[],
  clippingsDir: string,
  concurrency: number,
  aiClient: AIClient
): Promise<number> {
  await mkdir(clippingsDir, { recursive: true });
  const datePrefix = formatDateCompact();
  const used = new Set<string>();
  const tasks = articles.map(article => {
    let name = `${datePrefix}-${sanitizeFilename(article.title)}.md`;
    for (let n = 2; used.has(name); n++) name = `${datePrefix}-${sanitizeFilename(article.title)}-${n}.md`;
    used.add(name);
    return { article, name };
  });

  let saved = 0;
  await runWithConcurrency(tasks.length, concurrency, async (index) => {
    const { article, name } = tasks[index]!;
    const raw = await fetchArticleMarkdown(article.link);
    if (!raw) {
      console.warn(`[digest] ✗ Clipping failed: ${article.title}`);
      return;
    }
    await writeFile(`${clippingsDir}/${name}`, buildClippingMarkdown(article, await addBilingualTranslation(raw, aiClient)), 'utf8');
    saved++;
    console.log(`[digest] ✓ Clipping saved: ${name}`);
  });
  return saved;
}

import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Article, ArticleScoreBreakdown, RankedArticle } from './types.ts';

export type DigestStage = 'crawl' | 'score' | 'summarize' | 'highlights' | 'clippings' | 'done';

export interface SerializedArticle {
  title: string;
  link: string;
  pubDate: string;
  description: string;
  sourceName: string;
  sourceUrl: string;
}

export interface CheckpointData {
  version: 1;
  runKey: string;
  stage: DigestStage;
  updatedAt: string;
  config: { hours: number; topN: number; lang: 'zh' | 'en' };
  crawl: {
    doneFeedXmlUrls: string[];
    articles: SerializedArticle[];
    successCount: number;
    failCount: number;
    complete: boolean;
  };
  score: {
    candidateLinks: string[];
    byIndex: Record<string, ArticleScoreBreakdown>;
    complete: boolean;
  };
  top: Array<SerializedArticle & { totalScore: number; breakdown: ArticleScoreBreakdown }>;
  summaries: Record<string, { titleZh: string; summary: string; reason: string }>;
  summarizeComplete: boolean;
  highlights: string;
  highlightsComplete: boolean;
  reportComplete: boolean;
  clippingsDoneLinks: string[];
  clippingsComplete: boolean;
}

const DEFAULT_CACHE_DIR = fileURLToPath(new URL('../.cache/digest', import.meta.url));

export function buildRunKey(hours: number, topN: number, lang: string, date = new Date()): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}${m}${d}-h${hours}-n${topN}-${lang}`;
}

export function serializeArticle(a: Article): SerializedArticle {
  return {
    title: a.title,
    link: a.link,
    pubDate: a.pubDate.toISOString(),
    description: a.description,
    sourceName: a.sourceName,
    sourceUrl: a.sourceUrl,
  };
}

export function deserializeArticle(a: SerializedArticle): Article {
  return {
    title: a.title,
    link: a.link,
    pubDate: new Date(a.pubDate),
    description: a.description,
    sourceName: a.sourceName,
    sourceUrl: a.sourceUrl,
  };
}

export function serializeRanked(a: RankedArticle): CheckpointData['top'][number] {
  return { ...serializeArticle(a), totalScore: a.totalScore, breakdown: a.breakdown };
}

export function deserializeRanked(a: CheckpointData['top'][number]): RankedArticle {
  return { ...deserializeArticle(a), totalScore: a.totalScore, breakdown: a.breakdown };
}

function emptyCheckpoint(runKey: string, config: CheckpointData['config']): CheckpointData {
  return {
    version: 1,
    runKey,
    stage: 'crawl',
    updatedAt: new Date().toISOString(),
    config,
    crawl: {
      doneFeedXmlUrls: [],
      articles: [],
      successCount: 0,
      failCount: 0,
      complete: false,
    },
    score: { candidateLinks: [], byIndex: {}, complete: false },
    top: [],
    summaries: {},
    summarizeComplete: false,
    highlights: '',
    highlightsComplete: false,
    reportComplete: false,
    clippingsDoneLinks: [],
    clippingsComplete: false,
  };
}

export class CheckpointStore {
  readonly path: string;
  private data: CheckpointData;
  private writeChain: Promise<void> = Promise.resolve();
  private lock: Promise<void> = Promise.resolve();
  private writing: Promise<void> | null = null;
  private dirty = false;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFlushAt = 0;
  /** 连续更新时最多隔这么久落一次盘，避免定时器被反复重置而一直不写。 */
  private maxWaitMs = 2000;

  private constructor(path: string, data: CheckpointData) {
    this.path = path;
    this.data = data;
  }

  private async withLock<T>(fn: () => Promise<T>): Promise<T> {
    let release!: () => void;
    const prev = this.lock;
    this.lock = new Promise<void>(resolve => { release = resolve; });
    await prev;
    try {
      return await fn();
    } finally {
      release();
    }
  }

  static async open(options: {
    hours: number;
    topN: number;
    lang: 'zh' | 'en';
    cacheDir?: string;
    fresh?: boolean;
  }): Promise<CheckpointStore> {
    const cacheDir = options.cacheDir || process.env.DIGEST_CACHE_DIR?.trim() || DEFAULT_CACHE_DIR;
    const runKey = buildRunKey(options.hours, options.topN, options.lang);
    const path = join(cacheDir, `checkpoint-${runKey}.json`);
    await mkdir(dirname(path), { recursive: true });

    if (options.fresh) {
      const store = new CheckpointStore(path, emptyCheckpoint(runKey, {
        hours: options.hours,
        topN: options.topN,
        lang: options.lang,
      }));
      await store.save();
      console.log(`[digest] Fresh run, checkpoint: ${path}`);
      return store;
    }

    try {
      const raw = await readFile(path, 'utf8');
      const parsed = JSON.parse(raw) as CheckpointData;
      if (parsed.version === 1 && parsed.runKey === runKey && parsed.stage !== 'done') {
        const feeds = parsed.crawl.doneFeedXmlUrls.length;
        const scores = Object.keys(parsed.score.byIndex).length;
        const clippings = parsed.clippingsDoneLinks.length;
        console.log(
          `[digest] Resuming from checkpoint (${parsed.stage}): `
          + `feeds ${feeds}, scores ${scores}, clippings ${clippings} — ${path}`
        );
        return new CheckpointStore(path, parsed);
      }
      if (parsed.stage === 'done') {
        console.log(`[digest] Previous run complete, starting fresh checkpoint: ${path}`);
      }
    } catch {
      // no checkpoint
    }

    const store = new CheckpointStore(path, emptyCheckpoint(runKey, {
      hours: options.hours,
      topN: options.topN,
      lang: options.lang,
    }));
    await store.save();
    console.log(`[digest] Checkpoint: ${path}`);
    return store;
  }

  get state(): CheckpointData {
    return this.data;
  }

  async save(): Promise<void> {
    if (this.writing) {
      this.dirty = true;
      await this.writing;
      if (this.dirty) return this.save();
      return;
    }
    this.writing = this.persist();
    try {
      await this.writing;
    } finally {
      this.writing = null;
    }
    if (this.dirty) return this.save();
  }

  private async persist(): Promise<void> {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = null;
    }
    this.dirty = false;
    this.lastFlushAt = Date.now();
    this.data.updatedAt = new Date().toISOString();
    const payload = JSON.stringify(this.data);
    const tmp = `${this.path}.tmp`;
    this.writeChain = this.writeChain.then(async () => {
      await writeFile(tmp, payload, 'utf8');
      await rename(tmp, this.path);
    });
    await this.writeChain;
  }

  /** 高频进度更新用：合并落盘，但连续更新时也会按 maxWaitMs 写盘。 */
  saveSoon(): void {
    this.dirty = true;
    const elapsed = Date.now() - this.lastFlushAt;
    if (elapsed >= this.maxWaitMs) {
      void this.save();
      return;
    }
    if (this.debounceTimer) return;
    const delay = Math.max(50, this.maxWaitMs - elapsed);
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      void this.save();
    }, delay);
  }

  async flush(): Promise<void> {
    await this.save();
  }

  async setStage(stage: DigestStage): Promise<void> {
    this.data.stage = stage;
    await this.save();
  }

  async markDoneAndClear(): Promise<void> {
    this.data.stage = 'done';
    await this.save();
    try {
      await unlink(this.path);
      console.log(`[digest] Checkpoint cleared (run complete)`);
    } catch {
      // keep file if unlink fails
    }
  }

  async updateCrawl(mutator: (crawl: CheckpointData['crawl']) => void, immediate = false): Promise<void> {
    await this.withLock(async () => {
      mutator(this.data.crawl);
      if (immediate) await this.save();
      else this.saveSoon();
    });
  }

  async updateScore(mutator: (score: CheckpointData['score']) => void, immediate = false): Promise<void> {
    await this.withLock(async () => {
      mutator(this.data.score);
      if (immediate) await this.save();
      else this.saveSoon();
    });
  }

  async setTop(top: RankedArticle[]): Promise<void> {
    await this.withLock(async () => {
      this.data.top = top.map(serializeRanked);
      await this.save();
    });
  }

  async setSummary(index: number, value: { titleZh: string; summary: string; reason: string }): Promise<void> {
    await this.withLock(async () => {
      this.data.summaries[String(index)] = value;
      this.saveSoon();
    });
  }

  async setHighlights(text: string): Promise<void> {
    await this.withLock(async () => {
      this.data.highlights = text;
      this.data.highlightsComplete = true;
      await this.save();
    });
  }

  async markReportComplete(): Promise<void> {
    await this.withLock(async () => {
      this.data.reportComplete = true;
      await this.save();
    });
  }

  async markClippingDone(link: string): Promise<void> {
    await this.withLock(async () => {
      if (!this.data.clippingsDoneLinks.includes(link)) {
        this.data.clippingsDoneLinks.push(link);
        this.saveSoon();
      }
    });
  }
}

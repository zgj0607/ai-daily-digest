import type { RankedArticle } from './types.ts';

const MAX_PER_TOPIC = 2;
const KEYWORD_OVERLAP = 0.45;
const TITLE_OVERLAP = 0.5;
const TITLE_STOP = new Set([
  'a', 'an', 'the', 'is', 'are', 'was', 'were', 'to', 'of', 'in', 'on', 'for', 'with', 'and', 'or', 'how', 'why', 'what', 'new', 'its', 'it', 'as', 'that', 'this',
]);

export function normalizeTopicSlug(topic?: string): string {
  const slug = (topic || 'misc').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return slug || 'misc';
}

function jaccard(a: string[], b: string[]): number {
  const setA = new Set(a.map(v => v.toLowerCase()));
  const setB = new Set(b.map(v => v.toLowerCase()));
  if (!setA.size || !setB.size) return 0;
  let inter = 0;
  for (const v of setA) if (setB.has(v)) inter++;
  return inter / new Set([...setA, ...setB]).size;
}

function titleOverlap(a: string, b: string): number {
  const tokens = (s: string) => new Set(
    s.toLowerCase().replace(/[^a-z0-9\s-]/g, ' ').split(/\s+/).filter(w => w.length > 2 && !TITLE_STOP.has(w))
  );
  const setA = tokens(a);
  const setB = tokens(b);
  if (!setA.size || !setB.size) return 0;
  let inter = 0;
  for (const w of setA) if (setB.has(w)) inter++;
  return inter / Math.min(setA.size, setB.size);
}

function isDuplicate(candidate: RankedArticle, picked: RankedArticle[]): boolean {
  const topic = normalizeTopicSlug(candidate.breakdown.topic);
  for (const item of picked) {
    if (normalizeTopicSlug(item.breakdown.topic) !== topic) {
      if (jaccard(candidate.breakdown.keywords, item.breakdown.keywords) >= KEYWORD_OVERLAP) return true;
      if (titleOverlap(candidate.title, item.title) >= TITLE_OVERLAP) return true;
    }
  }
  return false;
}

export function selectDiverseTopArticles(
  articles: RankedArticle[],
  topN: number
): { selected: RankedArticle[]; skippedByDiversity: number } {
  const sorted = [...articles].sort((a, b) => b.totalScore - a.totalScore);
  const selected: RankedArticle[] = [];
  const topicCount = new Map<string, number>();
  let skippedByDiversity = 0;

  for (const article of sorted) {
    if (selected.length >= topN) break;
    const topic = normalizeTopicSlug(article.breakdown.topic);
    if ((topicCount.get(topic) || 0) >= MAX_PER_TOPIC || isDuplicate(article, selected)) {
      skippedByDiversity++;
      continue;
    }
    selected.push(article);
    topicCount.set(topic, (topicCount.get(topic) || 0) + 1);
  }

  if (selected.length < topN) {
    for (const article of sorted) {
      if (selected.length >= topN) break;
      if (!selected.some(item => item.link === article.link)) selected.push(article);
    }
  }

  return { selected, skippedByDiversity };
}

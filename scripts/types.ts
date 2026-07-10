export type CategoryId = 'ai-ml' | 'security' | 'engineering' | 'tools' | 'opinion' | 'other';

export interface FeedSource {
  name: string;
  xmlUrl: string;
  htmlUrl: string;
}

export interface Article {
  title: string;
  link: string;
  pubDate: Date;
  description: string;
  sourceName: string;
  sourceUrl: string;
}

export interface ArticleScoreBreakdown {
  relevance: number;
  quality: number;
  timeliness: number;
  category: CategoryId;
  keywords: string[];
  topic: string;
}

export interface RankedArticle extends Article {
  totalScore: number;
  breakdown: ArticleScoreBreakdown;
}

export interface ScoredArticle extends Article {
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

export interface AIClient {
  call(prompt: string): Promise<string>;
}

export const CATEGORY_META: Record<CategoryId, { emoji: string; label: string }> = {
  'ai-ml': { emoji: '🤖', label: 'AI / ML' },
  security: { emoji: '🔒', label: '安全' },
  engineering: { emoji: '⚙️', label: '工程' },
  tools: { emoji: '🛠', label: '工具 / 开源' },
  opinion: { emoji: '💡', label: '观点 / 杂谈' },
  other: { emoji: '📝', label: '其他' },
};

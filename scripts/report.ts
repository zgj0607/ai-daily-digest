import type { CategoryId, ScoredArticle } from './types.ts';
import { CATEGORY_META } from './types.ts';

function humanizeTime(pubDate: Date): string {
  const diffMs = Date.now() - pubDate.getTime();
  const mins = Math.floor(diffMs / 60_000);
  const hours = Math.floor(diffMs / 3_600_000);
  const days = Math.floor(diffMs / 86_400_000);
  if (mins < 60) return `${mins} 分钟前`;
  if (hours < 24) return `${hours} 小时前`;
  if (days < 7) return `${days} 天前`;
  return pubDate.toISOString().slice(0, 10);
}

function keywordCounts(articles: ScoredArticle[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const article of articles) {
    for (const kw of article.keywords) {
      const key = kw.toLowerCase();
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  return counts;
}

function topKeywords(articles: ScoredArticle[], limit: number) {
  return Array.from(keywordCounts(articles).entries()).sort((a, b) => b[1] - a[1]).slice(0, limit);
}

export function generateDigestReport(
  articles: ScoredArticle[],
  highlights: string,
  stats: { totalFeeds: number; successFeeds: number; totalArticles: number; filteredArticles: number; hours: number }
): string {
  const now = new Date();
  const dateStr = now.toISOString().split('T')[0];
  let report = `# 📰 AI 博客每日精选 — ${dateStr}\n\n> 来自 Karpathy 推荐和阮一峰网络日志出现的 ${stats.totalFeeds} 个顶级技术博客，AI 精选 Top ${articles.length}\n\n`;

  if (highlights) {
    report += `## 📝 今日行动指南\n\n${highlights}\n\n---\n\n`;
  }

  if (articles.length > 0) {
    report += `## 🏆 今日必读\n\n`;
    for (let i = 0; i < Math.min(10, articles.length); i++) {
      const a = articles[i]!;
      const medal = ['🥇', '🥈', '🥉'][i] || `${i + 1}.`;
      const cat = CATEGORY_META[a.category];
      report += `${medal} **${a.titleZh || a.title}**\n\n[${a.title}](${a.link}) — ${a.sourceName} · ${humanizeTime(a.pubDate)} · ${cat.emoji} ${cat.label}\n\n> ${a.summary}\n\n`;
      if (a.reason) report += `💡 **为什么值得读**: ${a.reason}\n\n`;
      if (a.keywords.length) report += `🏷️ ${a.keywords.join(', ')}\n\n`;
    }
    report += `---\n\n`;
  }

  report += `## 📊 数据概览\n\n| 扫描源 | 抓取文章 | 时间范围 | 精选 |\n|:---:|:---:|:---:|:---:|\n| ${stats.successFeeds}/${stats.totalFeeds} | ${stats.totalArticles} 篇 → ${stats.filteredArticles} 篇 | ${stats.hours}h | **${articles.length} 篇** |\n\n`;

  const catCount = new Map<CategoryId, number>();
  for (const a of articles) catCount.set(a.category, (catCount.get(a.category) || 0) + 1);
  if (catCount.size) {
    report += `### 分类分布\n\n\`\`\`mermaid\npie showData\n    title "文章分类分布"\n`;
    for (const [cat, count] of Array.from(catCount.entries()).sort((a, b) => b[1] - a[1])) {
      const meta = CATEGORY_META[cat];
      report += `    "${meta.emoji} ${meta.label}" : ${count}\n`;
    }
    report += '```\n\n';
  }

  const topKw = topKeywords(articles, 12);
  if (topKw.length) {
    report += `### 高频关键词\n\n\`\`\`mermaid\nxychart-beta horizontal\n    title "高频关键词"\n    x-axis [${topKw.map(([k]) => `"${k}"`).join(', ')}]\n    y-axis "出现次数" 0 --> ${topKw[0]![1] + 2}\n    bar [${topKw.map(([, v]) => v).join(', ')}]\n\`\`\`\n\n`;
    const top10 = topKeywords(articles, 10);
    const maxVal = top10[0]![1];
    const maxLabel = Math.max(...top10.map(([k]) => k.length));
    report += `<details>\n<summary>📈 纯文本关键词图（终端友好）</summary>\n\n\`\`\`\n`;
    for (const [label, value] of top10) {
      const barLen = Math.max(1, Math.round((value / maxVal) * 20));
      report += `${label.padEnd(maxLabel)} │ ${'█'.repeat(barLen)}${'░'.repeat(20 - barLen)} ${value}\n`;
    }
    report += '```\n</details>\n\n';
    report += `### 🏷️ 话题标签\n\n${topKw.slice(0, 20).map(([w, c], i) => i < 3 ? `**${w}**(${c})` : `${w}(${c})`).join(' · ')}\n\n`;
  }

  report += `---\n\n`;
  const groups = new Map<CategoryId, ScoredArticle[]>();
  for (const a of articles) {
    const list = groups.get(a.category) || [];
    list.push(a);
    groups.set(a.category, list);
  }

  let index = 0;
  for (const [catId, items] of Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length)) {
    const meta = CATEGORY_META[catId];
    report += `## ${meta.emoji} ${meta.label}\n\n`;
    for (const a of items) {
      index++;
      const score = a.scoreBreakdown.relevance + a.scoreBreakdown.quality + a.scoreBreakdown.timeliness;
      report += `### ${index}. ${a.titleZh || a.title}\n\n[${a.title}](${a.link}) — **${a.sourceName}** · ${humanizeTime(a.pubDate)} · ⭐ ${score}/30\n\n> ${a.summary}\n\n`;
      if (a.keywords.length) report += `🏷️ ${a.keywords.join(', ')}\n\n`;
      report += `---\n\n`;
    }
  }

  report += `*生成于 ${dateStr} ${now.toISOString().split('T')[1]?.slice(0, 5) || ''} | 扫描 ${stats.successFeeds} 源 → 获取 ${stats.totalArticles} 篇 → 精选 ${articles.length} 篇*\n`;
  report += `*基于 [Hacker News Popularity Contest 2025](https://refactoringenglish.com/tools/hn-popularity/) RSS 源列表，由 [Andrej Karpathy](https://x.com/karpathy) 和[阮一峰的网络日志](https://www.ruanyifeng.com/blog/)推荐]()*\n`;
  report += `*由「微谈小智」制作，欢迎关注同名微信公众号获取更多 AI 实用技巧 💡*\n`;
  return report;
}

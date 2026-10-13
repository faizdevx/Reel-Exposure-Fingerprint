import type { TopicShare } from './api';

export interface ShareRow {
  key: string;
  label: string;
  /** The topic's id, or null for the row that sums the topics not listed individually. */
  topicId: number | null;
  /** Percent with one decimal. All rows of one fingerprint add up to exactly 100.0. */
  percent: number;
  /** True for the row that sums the topics not listed individually. */
  isRest: boolean;
}

const SCALE = 10; // one decimal place: work in tenths of a percent

/**
 * Round fractions to tenths of a percent so the rounded values still add up to exactly 100.0
 * (largest remainder method). Plain rounding can give 99.9 or 100.1, which would contradict
 * "shares of one fingerprint sum to 100%".
 */
export function roundToTotal(fractions: readonly number[]): number[] {
  const total = fractions.reduce((a, b) => a + b, 0);
  if (fractions.length === 0 || !(total > 0)) return fractions.map(() => 0);
  const target = 100 * SCALE;
  const exact = fractions.map((f) => (f / total) * target);
  const floors = exact.map((x) => Math.floor(x));
  let missing = target - floors.reduce((a, b) => a + b, 0);
  const order = exact
    .map((x, i) => ({ i, rest: x - Math.floor(x) }))
    .sort((a, b) => b.rest - a.rest || a.i - b.i);
  for (const { i } of order) {
    if (missing <= 0) break;
    floors[i] = (floors[i] ?? 0) + 1;
    missing -= 1;
  }
  return floors.map((tenths) => tenths / SCALE);
}

/**
 * Rows for display: the largest topics individually, everything else as one "other topics" row,
 * so a long tail does not clutter the chart and the total is still 100%.
 */
export function toShareRows(topics: readonly TopicShare[], maxTopics = 8): ShareRow[] {
  const sorted = [...topics].sort((a, b) => b.share - a.share || a.topic_id - b.topic_id);
  const keepAll = sorted.length <= maxTopics + 1; // a lone "1 other topic" row would save nothing
  const head = keepAll ? sorted : sorted.slice(0, maxTopics);
  const tail = keepAll ? [] : sorted.slice(maxTopics);
  const fractions = head.map((t) => t.share);
  if (tail.length > 0) fractions.push(tail.reduce((sum, t) => sum + t.share, 0));
  const percents = roundToTotal(fractions);
  const rows: ShareRow[] = head.map((t, i) => ({
    key: t.slug,
    label: t.label,
    topicId: t.topic_id,
    percent: percents[i] ?? 0,
    isRest: false,
  }));
  if (tail.length > 0) {
    rows.push({
      key: '_rest',
      label: `${tail.length} other topics`,
      topicId: null,
      percent: percents[head.length] ?? 0,
      isRest: true,
    });
  }
  return rows;
}

export function formatPercent(percent: number): string {
  return `${percent.toFixed(1)}%`;
}

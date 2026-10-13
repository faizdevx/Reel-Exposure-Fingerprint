import { describe, expect, it } from 'vitest';

import type { TopicShare } from '../src/api';
import { roundToTotal, toShareRows } from '../src/shares';

function topics(shares: number[]): TopicShare[] {
  return shares.map((share, i) => ({ topic_id: i + 1, slug: `t${i + 1}`, label: `Topic ${i + 1}`, share }));
}

const tenths = (values: number[]) => values.reduce((sum, v) => sum + Math.round(v * 10), 0);

describe('roundToTotal', () => {
  it('adds up to exactly 100.0 where plain rounding would not', () => {
    // Three equal thirds round to 33.3 each = 99.9 with plain rounding.
    const out = roundToTotal([1 / 3, 1 / 3, 1 / 3]);
    expect(tenths(out)).toBe(1000);
    expect(out.slice().sort()).toEqual([33.3, 33.3, 33.4]);
  });

  it('gives the extra tenth to the largest remainder', () => {
    expect(roundToTotal([0.6666, 0.3334])).toEqual([66.7, 33.3]);
  });

  it('keeps already round values', () => {
    expect(roundToTotal([0.5, 0.3, 0.2])).toEqual([50, 30, 20]);
  });

  it('is zero for empty or all-zero input instead of inventing a split', () => {
    expect(roundToTotal([])).toEqual([]);
    expect(roundToTotal([0, 0])).toEqual([0, 0]);
  });

  it('adds up to 100.0 for many random-looking distributions', () => {
    for (let n = 1; n <= 40; n += 1) {
      const raw = Array.from({ length: n }, (_, i) => ((i * 7919 + n * 104729) % 997) + 1);
      const total = raw.reduce((a, b) => a + b, 0);
      expect(tenths(roundToTotal(raw.map((v) => v / total)))).toBe(1000);
    }
  });
});

describe('toShareRows', () => {
  it('sorts high to low and sums to 100', () => {
    const rows = toShareRows(topics([0.1, 0.6, 0.3]));
    expect(rows.map((r) => r.label)).toEqual(['Topic 2', 'Topic 3', 'Topic 1']);
    expect(rows.map((r) => r.percent)).toEqual([60, 30, 10]);
  });

  it('folds the tail into one row so the total stays 100', () => {
    const rows = toShareRows(topics(Array.from({ length: 20 }, () => 0.05)), 5);
    expect(rows).toHaveLength(6);
    expect(rows[5]).toMatchObject({ label: '15 other topics', isRest: true, percent: 75 });
    expect(tenths(rows.map((r) => r.percent))).toBe(1000);
  });

  it('does not fold a single leftover topic into "1 other topics"', () => {
    const rows = toShareRows(topics([0.3, 0.3, 0.2, 0.1, 0.1]), 4);
    expect(rows).toHaveLength(5);
    expect(rows.some((r) => r.isRest)).toBe(false);
  });

  it('is empty for no topics', () => {
    expect(toShareRows([])).toEqual([]);
  });

  it('carries the topic id on each topic row and none on the folded row', () => {
    const rows = toShareRows(topics(Array.from({ length: 20 }, () => 0.05)), 3);
    expect(rows.slice(0, 3).map((r) => r.topicId)).toEqual([1, 2, 3]);
    expect(rows[3]).toMatchObject({ isRest: true, topicId: null });
  });
});

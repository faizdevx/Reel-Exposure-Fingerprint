import { describe, expect, it } from 'vitest';

import type { Fingerprint, FingerprintHistory, HistoryPoint, SessionSummary } from '../src/api';
import {
  biggestGaps,
  buildComparison,
  buildShareRows,
  buildTimeline,
  formatGap,
  leadingTopic,
  rankTopics,
  seriesKeys,
  utcDay,
} from '../src/series';

function topics(n: number) {
  return Array.from({ length: n }, (_, i) => ({ topic_id: i + 1, slug: `t${i + 1}`, label: `Topic ${i + 1}` }));
}

function point(day: number, shares: Record<number, number>, extra: Partial<HistoryPoint> = {}): HistoryPoint {
  return {
    taken_at: `2026-10-${String(day).padStart(2, '0')}T20:00:00Z`,
    session_id: null,
    n_reels: day * 10,
    reels_in_bucket: 10,
    shares: Object.fromEntries(Object.entries(shares).map(([k, v]) => [k, v])),
    ...extra,
  };
}

function history(points: HistoryPoint[], n = 4, kind: 'exposure' | 'engagement' = 'exposure'): FingerprintHistory {
  return { kind, bucket: 'day', pipeline_version: 'fp1-x', topics: topics(n), points };
}

function fingerprint(kind: 'exposure' | 'engagement', shares: Record<number, number>): Fingerprint {
  return {
    kind,
    taken_at: '2026-10-01T12:00:00Z',
    n_reels: 10,
    pipeline_version: 'fp1-x',
    session_id: null,
    topics: Object.entries(shares).map(([id, share]) => ({
      topic_id: Number(id),
      slug: `t${id}`,
      label: `Topic ${id}`,
      share,
    })),
  };
}

const sum = (values: number[]) => Math.round(values.reduce((a, b) => a + b, 0) * 10) / 10;

describe('topic share over time', () => {
  const h = history([
    point(1, { 1: 1 / 3, 2: 1 / 3, 3: 1 / 3 }),
    point(2, { 1: 0.5, 2: 0.3, 3: 0.2 }),
    point(3, { 1: 0.7, 2: 0.2, 3: 0.1 }),
  ]);

  it('gives every day a row that adds up to exactly 100.0, even for thirds', () => {
    const rows = buildShareRows(h, rankTopics([h]));
    expect(rows).toHaveLength(3);
    for (const row of rows) expect(sum(Object.values(row.values))).toBe(100);
  });

  it('is ordered by time whatever the order the points came in', () => {
    const [a, b, c] = h.points;
    if (!a || !b || !c) throw new Error('fixture');
    const shuffled = history([c, a, b]);
    const dates = buildShareRows(shuffled, rankTopics([shuffled])).map((r) => r.date);
    expect(dates).toEqual(['2026-10-01', '2026-10-02', '2026-10-03']);
  });

  it('gives the top topics their own series in a fixed colour order and folds the rest', () => {
    const many = history([point(1, { 1: 0.3, 2: 0.2, 3: 0.15, 4: 0.1, 5: 0.1, 6: 0.05, 7: 0.05, 8: 0.05 })], 8);
    const ranking = rankTopics([many]);
    const keys = seriesKeys(ranking);
    expect(keys.map((k) => k.key)).toEqual(['t1', 't2', 't3', 't4', 't5', 't6', 'rest']);
    expect(keys.map((k) => k.color)).toEqual([
      'var(--series-1)',
      'var(--series-2)',
      'var(--series-3)',
      'var(--series-4)',
      'var(--series-5)',
      'var(--series-6)',
      'var(--muted)',
    ]);
    const [row] = buildShareRows(many, ranking);
    expect(row?.values['rest']).toBe(10);
    expect(sum(Object.values(row?.values ?? {}))).toBe(100);
  });

  it('has no rest series when every topic fits', () => {
    expect(seriesKeys(rankTopics([h])).some((k) => k.isRest)).toBe(false);
  });

  it('keeps a topic on the same colour when switching between exposure and engagement', () => {
    const exposure = history([point(1, { 1: 0.6, 2: 0.3, 3: 0.1 })], 3, 'exposure');
    const engagement = history([point(1, { 3: 0.6, 2: 0.3, 1: 0.1 })], 3, 'engagement');
    const ranking = rankTopics([exposure, engagement]);
    const colours = Object.fromEntries(seriesKeys(ranking).map((k) => [k.key, k.color]));
    // The same ranking serves both kinds, so a key's colour cannot differ between them.
    expect(buildShareRows(exposure, ranking)[0]?.values).toHaveProperty('t3');
    expect(buildShareRows(engagement, ranking)[0]?.values).toHaveProperty('t3');
    expect(colours['t3']).toBe(seriesKeys(ranking).find((k) => k.key === 't3')?.color);
    expect(Object.keys(colours)).toEqual(expect.arrayContaining(['t1', 't2', 't3']));
  });

  it('treats a topic missing from a day as zero, not as missing', () => {
    const sparse = history([point(1, { 1: 1 }), point(2, { 2: 1 })], 2);
    const rows = buildShareRows(sparse, rankTopics([sparse]));
    expect(rows[0]?.values).toEqual({ t1: 100, t2: 0 });
    expect(rows[1]?.values).toEqual({ t1: 0, t2: 100 });
  });

  it('reads the UTC day from the timestamp', () => {
    expect(utcDay('2026-10-01T23:59:59Z')).toBe('2026-10-01');
    expect(utcDay('2026-10-02T04:00:00+05:30')).toBe('2026-10-01'); // 22:30 UTC the day before
    expect(utcDay('nonsense')).toBe('unknown');
  });
});

describe('exposure versus engagement', () => {
  const exposure = fingerprint('exposure', { 1: 0.5, 2: 0.3, 3: 0.2 });
  const engagement = fingerprint('engagement', { 1: 0.2, 2: 0.3, 3: 0.5 });

  it('rounds each fingerprint on its own so each adds up to 100.0', () => {
    const rows = buildComparison(exposure, engagement);
    expect(sum(rows.map((r) => r.exposure))).toBe(100);
    expect(sum(rows.map((r) => r.engagement))).toBe(100);
    expect(sum(rows.map((r) => r.gap))).toBe(0);
  });

  it('reports the gap as engagement minus exposure, from the displayed values', () => {
    const rows = buildComparison(exposure, engagement);
    const t1 = rows.find((r) => r.label === 'Topic 1');
    expect(t1).toMatchObject({ exposure: 50, engagement: 20, gap: -30 });
    const gaps = biggestGaps(rows);
    expect(gaps.over?.label).toBe('Topic 3');
    expect(gaps.under?.label).toBe('Topic 1');
  });

  it('puts a topic missing from one fingerprint at zero there', () => {
    const rows = buildComparison(fingerprint('exposure', { 1: 1 }), fingerprint('engagement', { 2: 1 }));
    expect(rows.find((r) => r.label === 'Topic 1')).toMatchObject({ exposure: 100, engagement: 0 });
    expect(rows.find((r) => r.label === 'Topic 2')).toMatchObject({ exposure: 0, engagement: 100 });
  });

  it('folds the tail into one row and keeps both totals at 100', () => {
    const shares = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [i + 1, 0.05]));
    const rows = buildComparison(fingerprint('exposure', shares), fingerprint('engagement', shares), 5);
    expect(rows).toHaveLength(6);
    expect(rows.at(-1)).toMatchObject({ label: '15 other topics', isRest: true, exposure: 75 });
    expect(sum(rows.map((r) => r.engagement))).toBe(100);
  });

  it('is empty without topics, and has no "biggest gap" when nothing differs', () => {
    expect(buildComparison(fingerprint('exposure', {}), fingerprint('engagement', {}))).toEqual([]);
    const same = buildComparison(exposure, exposure);
    expect(biggestGaps(same)).toEqual({ over: null, under: null });
  });

  it('formats gaps with a sign and a real minus', () => {
    expect(formatGap(3.2)).toBe('+3.2');
    expect(formatGap(-9.1)).toBe('−9.1');
    expect(formatGap(0)).toBe('0.0');
  });
});

describe('session timeline and leaders', () => {
  const session = (id: string, start: string, end: string | null, reels: number | null): SessionSummary => ({
    id,
    started_at: start,
    ended_at: end,
    status: end === null ? 'active' : 'stopped',
    stop_reason: end === null ? null : 'user',
    n_reels: reels,
  });

  it('puts sessions in time order and joins each to the snapshot taken after it', () => {
    const h = history(
      [
        point(2, { 1: 0.2, 2: 0.8 }, { session_id: 's2' }),
        point(1, { 1: 0.9, 2: 0.1 }, { session_id: 's1' }),
      ],
      2,
    );
    const items = buildTimeline(
      [
        session('s2', '2026-10-02T10:00:00Z', '2026-10-02T10:30:00Z', 8),
        session('s1', '2026-10-01T10:00:00Z', '2026-10-01T10:12:00Z', 5),
        session('s3', '2026-10-03T10:00:00Z', null, null),
      ],
      h,
    );
    expect(items.map((i) => i.id)).toEqual(['s1', 's2', 's3']);
    expect(items.map((i) => i.minutes)).toEqual([12, 30, null]);
    expect(items[0]?.leader).toEqual({ label: 'Topic 1', percent: 90 });
    expect(items[1]?.leader).toEqual({ label: 'Topic 2', percent: 80 });
    expect(items[2]?.leader).toBeNull(); // not processed: no made-up leader
    expect(items[2]?.reels).toBeNull();
  });

  it('works without any session snapshots', () => {
    const items = buildTimeline([session('s1', '2026-10-01T10:00:00Z', '2026-10-01T10:05:00Z', 3)], undefined);
    expect(items[0]?.leader).toBeNull();
  });

  it('breaks leader ties toward the lower topic id', () => {
    const h = history([point(1, { 1: 0.5, 2: 0.5 })], 2);
    const first = h.points[0];
    if (!first) throw new Error('fixture');
    expect(leadingTopic(first, h)?.label).toBe('Topic 1');
    expect(leadingTopic(point(1, {}), h)).toBeNull();
  });
});

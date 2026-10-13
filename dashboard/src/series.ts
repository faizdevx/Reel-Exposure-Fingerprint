import type { Fingerprint, FingerprintHistory, HistoryPoint, SessionSummary } from './api';
import { roundToTotal } from './shares';

/** Categorical colours, in the fixed order of the palette; the rest of the topics is muted. */
export const SERIES_COLORS = [
  'var(--series-1)',
  'var(--series-2)',
  'var(--series-3)',
  'var(--series-4)',
  'var(--series-5)',
  'var(--series-6)',
] as const;
export const REST_COLOR = 'var(--muted)';

export const MAX_SERIES = SERIES_COLORS.length;

export interface SeriesKey {
  key: string;
  label: string;
  color: string;
  isRest: boolean;
}

export interface ShareRow {
  /** UTC calendar day, `YYYY-MM-DD`. */
  date: string;
  takenAt: string;
  /** Reels counted so far. */
  reels: number;
  /** Reels counted inside the day. */
  reelsInDay: number | null;
  /** Series key to percent with one decimal; the values of one row add up to exactly 100.0. */
  values: Record<string, number>;
}

export interface Ranking {
  topIds: number[];
  labels: Map<number, string>;
  /** True when some topic is not among the top ones, so a "rest" series is needed. */
  hasRest: boolean;
}

export function utcDay(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? 'unknown' : date.toISOString().slice(0, 10);
}

function meanShares(history: FingerprintHistory): Map<number, number> {
  const sums = new Map<number, number>();
  for (const point of history.points) {
    for (const [id, share] of Object.entries(point.shares)) {
      sums.set(Number(id), (sums.get(Number(id)) ?? 0) + share);
    }
  }
  const n = Math.max(1, history.points.length);
  return new Map([...sums].map(([id, total]) => [id, total / n]));
}

/**
 * Which topics get their own colour. The ranking uses every history passed in (exposure and
 * engagement together), so a topic keeps its colour when the reader switches between them: colour
 * follows the topic, not its rank in whichever fingerprint is on screen.
 */
export function rankTopics(histories: readonly FingerprintHistory[], max = MAX_SERIES): Ranking {
  const labels = new Map<number, string>();
  const score = new Map<number, number>();
  for (const history of histories) {
    for (const topic of history.topics) labels.set(topic.topic_id, topic.label);
    for (const [id, mean] of meanShares(history)) score.set(id, (score.get(id) ?? 0) + mean);
  }
  const ids = [...score.keys()].sort((a, b) => (score.get(b) ?? 0) - (score.get(a) ?? 0) || a - b);
  return { topIds: ids.slice(0, max), labels, hasRest: ids.length > max };
}

export function seriesKeys(ranking: Ranking): SeriesKey[] {
  const keys: SeriesKey[] = ranking.topIds.map((id, i) => ({
    key: `t${id}`,
    label: ranking.labels.get(id) ?? `Topic ${id}`,
    color: SERIES_COLORS[i] ?? REST_COLOR,
    isRest: false,
  }));
  if (ranking.hasRest) {
    keys.push({ key: 'rest', label: 'All other topics', color: REST_COLOR, isRest: true });
  }
  return keys;
}

function rowFor(point: HistoryPoint, ranking: Ranking): ShareRow {
  const top = ranking.topIds.map((id) => point.shares[String(id)] ?? 0);
  const topTotal = top.reduce((a, b) => a + b, 0);
  const fractions = ranking.hasRest ? [...top, Math.max(0, 1 - topTotal)] : top;
  const percents = roundToTotal(fractions);
  const values: Record<string, number> = {};
  ranking.topIds.forEach((id, i) => {
    values[`t${id}`] = percents[i] ?? 0;
  });
  if (ranking.hasRest) values['rest'] = percents[ranking.topIds.length] ?? 0;
  return {
    date: utcDay(point.taken_at),
    takenAt: point.taken_at,
    reels: point.n_reels,
    reelsInDay: point.reels_in_bucket,
    values,
  };
}

/** Topic share over time: one row per snapshot, oldest first, every row adding up to 100.0. */
export function buildShareRows(history: FingerprintHistory, ranking: Ranking): ShareRow[] {
  return [...history.points]
    .sort((a, b) => a.taken_at.localeCompare(b.taken_at))
    .map((point) => rowFor(point, ranking));
}

// ---------------------------------------------------------------- exposure versus engagement

export interface CompareRow {
  key: string;
  label: string;
  exposure: number;
  engagement: number;
  /** Engagement minus exposure, in percentage points, from the displayed values. */
  gap: number;
  isRest: boolean;
}

function shareOf(fp: Fingerprint, id: number): number {
  return fp.topics.find((t) => t.topic_id === id)?.share ?? 0;
}

/**
 * Both fingerprints over the same topics, so the bars line up. Each fingerprint is rounded on its
 * own to add up to 100.0, so neither side shows independent percentages. The topics outside the
 * top ones are folded into one row.
 */
export function buildComparison(
  exposure: Fingerprint,
  engagement: Fingerprint,
  max = 8,
): CompareRow[] {
  const ids = new Set<number>([
    ...exposure.topics.map((t) => t.topic_id),
    ...engagement.topics.map((t) => t.topic_id),
  ]);
  if (ids.size === 0) return [];
  const labels = new Map<number, string>();
  for (const t of [...exposure.topics, ...engagement.topics]) labels.set(t.topic_id, t.label);
  const ordered = [...ids].sort(
    (a, b) =>
      Math.max(shareOf(exposure, b), shareOf(engagement, b)) -
        Math.max(shareOf(exposure, a), shareOf(engagement, a)) || a - b,
  );
  const keepAll = ordered.length <= max + 1;
  const head = keepAll ? ordered : ordered.slice(0, max);
  const tail = keepAll ? [] : ordered.slice(max);
  const fold = (fp: Fingerprint) => {
    const values = head.map((id) => shareOf(fp, id));
    if (tail.length > 0) values.push(tail.reduce((sum, id) => sum + shareOf(fp, id), 0));
    return roundToTotal(values);
  };
  const e = fold(exposure);
  const g = fold(engagement);
  const rows: CompareRow[] = head.map((id, i) => ({
    key: `t${id}`,
    label: labels.get(id) ?? `Topic ${id}`,
    exposure: e[i] ?? 0,
    engagement: g[i] ?? 0,
    gap: Math.round(((g[i] ?? 0) - (e[i] ?? 0)) * 10) / 10,
    isRest: false,
  }));
  if (tail.length > 0) {
    const i = head.length;
    rows.push({
      key: 'rest',
      label: `${tail.length} other topics`,
      exposure: e[i] ?? 0,
      engagement: g[i] ?? 0,
      gap: Math.round(((g[i] ?? 0) - (e[i] ?? 0)) * 10) / 10,
      isRest: true,
    });
  }
  return rows;
}

export function formatGap(gap: number): string {
  if (gap === 0) return '0.0';
  return `${gap > 0 ? '+' : '−'}${Math.abs(gap).toFixed(1)}`;
}

/** The biggest gap each way, among real topics: where the feed and the user's time disagree. */
export function biggestGaps(rows: readonly CompareRow[]): {
  over: CompareRow | null;
  under: CompareRow | null;
} {
  const topics = rows.filter((r) => !r.isRest);
  const over = topics.reduce<CompareRow | null>((a, r) => (r.gap > (a?.gap ?? 0) ? r : a), null);
  const under = topics.reduce<CompareRow | null>((a, r) => (r.gap < (a?.gap ?? 0) ? r : a), null);
  return { over, under };
}

// ---------------------------------------------------------------- daily snapshots and sessions

export interface Leader {
  label: string;
  /** Share of the fingerprint, percent with one decimal. */
  percent: number;
}

export function leadingTopic(point: HistoryPoint, history: FingerprintHistory): Leader | null {
  let best: [number, number] | null = null;
  for (const [id, share] of Object.entries(point.shares)) {
    const n = Number(id);
    if (best === null || share > best[1] || (share === best[1] && n < best[0])) best = [n, share];
  }
  if (best === null) return null;
  const label = history.topics.find((t) => t.topic_id === best?.[0])?.label ?? `Topic ${best[0]}`;
  return { label, percent: Math.round(best[1] * 1000) / 10 };
}

export interface TimelineItem {
  id: string;
  startedAt: string;
  endedAt: string | null;
  minutes: number | null;
  /** Reels counted in this session; null until it has been processed. */
  reels: number | null;
  status: string;
  /** The leading topic of the exposure fingerprint right after this session. */
  leader: Leader | null;
}

export function buildTimeline(
  sessions: readonly SessionSummary[],
  history: FingerprintHistory | undefined,
): TimelineItem[] {
  const bySession = new Map<string, HistoryPoint>();
  for (const point of history?.points ?? []) {
    if (point.session_id) bySession.set(point.session_id, point);
  }
  return [...sessions]
    .sort((a, b) => a.started_at.localeCompare(b.started_at) || a.id.localeCompare(b.id))
    .map((s) => {
      const end = s.ended_at === null ? null : new Date(s.ended_at).getTime();
      const start = new Date(s.started_at).getTime();
      const point = bySession.get(s.id);
      return {
        id: s.id,
        startedAt: s.started_at,
        endedAt: s.ended_at,
        minutes: end === null || end < start ? null : Math.floor((end - start) / 60_000),
        reels: s.n_reels,
        status: s.status,
        leader: point && history ? leadingTopic(point, history) : null,
      };
    });
}

import type { Page, Route } from '@playwright/test';

export interface Topic {
  topic_id: number;
  slug: string;
  label: string;
  share: number;
}

export interface MockSession {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  stop_reason: string | null;
  n_reels: number | null;
}

export interface MockHistoryPoint {
  taken_at: string;
  session_id: string | null;
  n_reels: number;
  reels_in_bucket: number | null;
  shares: Record<string, number>;
}

export interface MockHistory {
  topics: { topic_id: number; slug: string; label: string }[];
  day?: MockHistoryPoint[];
  session?: MockHistoryPoint[];
}

export interface MockEvidenceRequest {
  topicId: number;
  kind: string;
  limit: number;
  offset: number;
}

export interface MockDeletion {
  /** Answer the first N deletion calls with an incomplete verification (HTTP 500). */
  incompleteFirst?: number;
  /** What the incomplete answer says is still there. */
  remaining?: Record<string, number>;
}

export interface MockOptions {
  /** State of the self-report survey; the default is already submitted. */
  survey?: 'pending' | 'submitted' | 'skipped';
  failSurveySave?: boolean;
  /** Body for GET /predictions/next; null answers 500; leave out for "no predictions yet". */
  prediction?: unknown;
  failPredictionFirst?: number;
  deletion?: MockDeletion;
  /** Answers GET /topics/{id}/evidence; return null for a 500. */
  evidence?: (request: MockEvidenceRequest) => unknown;
  /** Snapshot history per fingerprint kind, for GET /fingerprint/history. */
  history?: { exposure?: MockHistory; engagement?: MockHistory };
  failHistory?: boolean;
  loggedIn?: boolean;
  exposure?: Topic[];
  engagement?: Topic[];
  sessions?: MockSession[];
  /** Milliseconds to hold every data response, to observe loading states. */
  delayMs?: number;
  /** Fail the next N exposure fingerprint requests with 500 (the page retries a 500 twice). */
  failFingerprints?: number;
  failSessions?: boolean;
  /** Credentials the fake login accepts. */
  password?: string;
}

export const EMAIL = 'alice@example.test';
export const PASSWORD = 'correct horse battery';

export function topics(shares: Record<string, number>): Topic[] {
  return Object.entries(shares).map(([label, share], i) => ({
    topic_id: i + 1,
    slug: label.toLowerCase().replace(/\W+/g, '-'),
    label,
    share,
  }));
}

function fingerprint(kind: string, list: Topic[]) {
  return {
    kind,
    taken_at: list.length > 0 ? '2026-10-01T12:00:00Z' : null,
    n_reels: list.length > 0 ? 42 : 0,
    pipeline_version: 'fp1-test',
    session_id: null,
    topics: list,
  };
}

/**
 * Stateful fake of the dashboard API. Data endpoints answer 401 until `POST /auth/login` succeeds,
 * like the real server, so the redirect-to-login flow is exercised for real.
 */
export async function mockApi(page: Page, options: MockOptions = {}) {
  const state = {
    loggedIn: options.loggedIn ?? false,
    failFingerprints: options.failFingerprints ?? 0,
    loginBodies: [] as unknown[],
    loginHeaders: [] as Record<string, string>[],
    requests: [] as string[],
    sessions: [...(options.sessions ?? [])],
    deletions: [] as { url: string; method: string; csrf: string | undefined }[],
    incomplete: options.deletion?.incompleteFirst ?? 0,
    surveyWrites: [] as { method: string; url: string; body: string | null }[],
  };

  const reply = async (route: Route, status: number, body: unknown) => {
    if (options.delayMs) await new Promise((resolve) => setTimeout(resolve, options.delayMs));
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  };

  await page.route('**/healthz', (route) => reply(route, 200, { status: 'ok' }));

  await page.route('**/api/v1/auth/login', async (route) => {
    const request = route.request();
    const body = request.postDataJSON() as { email: string; password: string };
    state.loginBodies.push(body);
    state.loginHeaders.push(request.headers());
    if (body.email === EMAIL && body.password === (options.password ?? PASSWORD)) {
      state.loggedIn = true;
      await reply(route, 200, { id: 'u1', email: EMAIL });
    } else {
      await reply(route, 401, { detail: 'Invalid email or password' });
    }
  });

  await page.route('**/api/v1/auth/logout', async (route) => {
    state.loggedIn = false;
    await route.fulfill({ status: 204 });
  });

  await page.route('**/api/v1/fingerprint/current?*', async (route) => {
    state.requests.push(route.request().url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    const kind = new URL(route.request().url()).searchParams.get('kind') ?? '';
    if (kind === 'exposure' && state.failFingerprints > 0) {
      state.failFingerprints -= 1;
      return reply(route, 500, { detail: 'boom' });
    }
    const list = (kind === 'engagement' ? options.engagement : options.exposure) ?? [];
    return reply(route, 200, fingerprint(kind, list));
  });

  await page.route('**/api/v1/fingerprint/history?*', async (route) => {
    state.requests.push(route.request().url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    if (options.failHistory) return reply(route, 500, { detail: 'boom' });
    const url = new URL(route.request().url());
    const kind = url.searchParams.get('kind') === 'engagement' ? 'engagement' : 'exposure';
    const bucket = url.searchParams.get('bucket') === 'session' ? 'session' : 'day';
    const source = options.history?.[kind];
    const limit = Number(url.searchParams.get('limit') ?? 90);
    const points = (source?.[bucket] ?? []).slice(-limit);
    return reply(route, 200, {
      kind,
      bucket,
      pipeline_version: source ? 'fp1-test' : null,
      topics: source?.topics ?? [],
      points,
    });
  });

  await page.route('**/api/v1/topics/*/evidence?*', async (route) => {
    state.requests.push(route.request().url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    const url = new URL(route.request().url());
    const topicId = Number(url.pathname.split('/').at(-2));
    const body = options.evidence?.({
      topicId,
      kind: url.searchParams.get('kind') ?? '',
      limit: Number(url.searchParams.get('limit') ?? 20),
      offset: Number(url.searchParams.get('offset') ?? 0),
    });
    if (body === undefined || body === null) return reply(route, 500, { detail: 'boom' });
    return reply(route, 200, body);
  });

  const deletionReport = (scope: 'session' | 'all', remaining: Record<string, number>) => ({
    scope,
    existed: true,
    deleted: scope === 'session' ? { sessions: 1, reel_exposures: 12, jobs: 12 } : { users: 1, sessions: 3, reel_exposures: 40 },
    remaining: { sessions: 0, reel_exposures: 0, jobs: 0, frame_files: 0, ...remaining },
    frame_files_deleted: 24,
    fingerprints_rebuilt: scope === 'session' ? 2 : 0,
    fingerprints_dropped: 0,
    verified: Object.values(remaining).every((v) => v === 0),
  });

  const answerDeletion = async (route: Route, scope: 'session' | 'all') => {
    const request = route.request();
    state.deletions.push({ url: request.url(), method: request.method(), csrf: request.headers()['x-requested-with'] });
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    if (state.incomplete > 0) {
      state.incomplete -= 1;
      const left = options.deletion?.remaining ?? { frame_files: 3 };
      return reply(route, 500, { detail: 'deletion_incomplete', ...deletionReport(scope, left) });
    }
    if (scope === 'all') state.loggedIn = false;
    return reply(route, 200, deletionReport(scope, {}));
  };

  await page.route('**/api/v1/me/data', (route) => answerDeletion(route, 'all'));

  const surveyTopics = [
    { topic_id: 1, slug: 'cricket', label: 'Cricket' },
    { topic_id: 2, slug: 'tech', label: 'Tech' },
    { topic_id: 3, slug: 'food', label: 'Food' },
  ];
  let surveyStatus = options.survey ?? 'submitted';
  let surveyRatings: Record<string, number> = surveyStatus === 'submitted' ? { '1': 5, '2': 3, '3': 1 } : {};
  const surveyBody = () => ({
    status: surveyStatus,
    completed_at: surveyStatus === 'pending' ? null : '2026-10-01T10:00:00Z',
    topics: surveyTopics,
    ratings: surveyRatings,
  });
  await page.route('**/api/v1/self-report**', async (route) => {
    const request = route.request();
    state.requests.push(request.url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    if (request.method() === 'GET') return reply(route, 200, surveyBody());
    if (options.failSurveySave) return reply(route, 500, { detail: 'boom' });
    state.surveyWrites.push({ method: request.method(), url: request.url(), body: request.postData() });
    if (request.url().endsWith('/skip')) {
      surveyStatus = 'skipped';
    } else {
      surveyRatings = (request.postDataJSON() as { ratings: Record<string, number> }).ratings;
      surveyStatus = 'submitted';
    }
    return reply(route, 200, surveyBody());
  });

  let predictionFailures = options.failPredictionFirst ?? 0;
  await page.route('**/api/v1/predictions/next', async (route) => {
    state.requests.push(route.request().url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    if (predictionFailures > 0) {
      predictionFailures -= 1;
      return reply(route, 500, { detail: 'boom' });
    }
    if (options.prediction === null) return reply(route, 500, { detail: 'boom' });
    return reply(route, 200, options.prediction ?? noPrediction());
  });

  await page.route('**/api/v1/sessions/*', async (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    const id = new URL(route.request().url()).pathname.split('/').at(-1);
    const response = await answerDeletion(route, 'session');
    if (state.incomplete <= 0 && state.loggedIn) {
      state.sessions = state.sessions.filter((session) => session.id !== id);
    }
    return response;
  });

  await page.route('**/api/v1/sessions', async (route) => {
    state.requests.push(route.request().url());
    if (!state.loggedIn) return reply(route, 401, { detail: 'Not authenticated' });
    if (options.failSessions) return reply(route, 500, { detail: 'boom' });
    return reply(route, 200, state.sessions);
  });

  return state;
}

const NAMES = [
  'Cricket',
  'Tech and coding',
  'Food',
  'Travel',
  'Music',
  'Comedy',
  'Fitness',
  'Fashion',
  'Gaming',
];

/**
 * `days` snapshots, one per UTC day from 2026-10-01, over the first `topicCount` topics. Exposure
 * leans to the first topics and engagement to the last ones, drifting a little each day. Shares
 * of every point add up to 1.
 */
export function makeHistory(
  kind: 'exposure' | 'engagement',
  days: number,
  topicCount = NAMES.length,
): MockHistory {
  const topics = NAMES.slice(0, topicCount).map((label, i) => ({
    topic_id: i + 1,
    slug: label.toLowerCase().replace(/\W+/g, '-'),
    label,
  }));
  const points: MockHistoryPoint[] = Array.from({ length: days }, (_, d) => {
    const raw = topics.map((_t, i) => {
      const lean = kind === 'exposure' ? topicCount - i : i + 1;
      return lean + ((d + i) % 3) * 0.4;
    });
    const total = raw.reduce((a, b) => a + b, 0);
    return {
      taken_at: `2026-10-${String(d + 1).padStart(2, '0')}T20:00:00Z`,
      session_id: null,
      n_reels: (d + 1) * 12,
      reels_in_bucket: 12,
      shares: Object.fromEntries(raw.map((v, i) => [String(i + 1), v / total])),
    };
  });
  return { topics, day: points };
}

/** Session snapshots matching `ids`, one per session, for the timeline's "led by" line. */
export function withSessionSnapshots(history: MockHistory, ids: string[]): MockHistory {
  const base = history.day ?? [];
  const session = ids.map((id, i) => {
    const source = base[Math.min(i, base.length - 1)];
    if (!source) throw new Error('need at least one day of history');
    return { ...source, session_id: id };
  });
  return { ...history, session };
}

/**
 * A paged evidence answer for one topic. Reel `i` (0-based, biggest first) has contribution
 * `1 / (i + 2)`, so the list is already in order; the numbers are made up for layout and paging,
 * and the sum in `explanation` is simply the sum of all of them.
 */
export function makeEvidence(
  request: MockEvidenceRequest,
  options: { total: number; label?: string; weight?: number } = { total: 25 },
) {
  const { total } = options;
  const label = options.label ?? 'Cricket';
  const contributions = Array.from({ length: total }, (_, i) => 1 / (i + 2));
  const sum = contributions.reduce((a, b) => a + b, 0);
  const items = contributions.slice(request.offset, request.offset + request.limit).map((c, k) => {
    const i = request.offset + k;
    return {
      event_id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
      session_id: '11111111-1111-4111-8111-111111111111',
      reel_shortcode: `Creel${String(i).padStart(3, '0')}`,
      reel_url: `https://www.instagram.com/reel/Creel${String(i).padStart(3, '0')}/`,
      caption: i % 4 === 3 ? null : `Caption of reel number ${i}`,
      caption_truncated: i === 1,
      observed_at: `2026-10-0${(i % 9) + 1}T18:30:00Z`,
      dwell_ms: i % 5 === 4 ? null : 8_000 + i * 100,
      video_duration_ms: 15_000,
      topic_score: 0.6,
      p_text: 0.7,
      p_visual: i % 2 === 0 ? 0.5 : null,
      gain: 1,
      recency: 0.9,
      contribution: c,
      share_of_topic: c / sum,
    };
  });
  const end = request.offset + items.length;
  return {
    topic: { id: request.topicId, slug: label.toLowerCase(), label },
    kind: request.kind,
    evidence_available: true,
    reason: null,
    fingerprint: {
      taken_at: '2026-10-09T12:00:00Z',
      pipeline_version: 'fp1-test',
      n_reels: 80,
      half_life_days: 7,
      topic_weight: options.weight ?? sum,
      total_weight: (options.weight ?? sum) * 2.5,
      topic_share: 0.4,
    },
    explanation: {
      gain_rule:
        request.kind === 'engagement'
          ? 'A Reel counts by how long you stayed: dwell divided by the Reel length.'
          : 'Every Reel the feed showed you counts the same, once (gain 1).',
      formula: 'contribution = topic score x gain x recency.',
      contributing_reels: total,
      contributions_sum: sum,
      coverage: sum / (options.weight ?? sum),
    },
    total,
    limit: request.limit,
    offset: request.offset,
    next_offset: end < total ? end : null,
    items,
  };
}

export function noPrediction() {
  return {
    label: 'Predicted next observed topic',
    note: 'A statistical guess at the topic of the next Reel the feed shows you, from your own history. It is not a prediction of what you like.',
    available: false,
    reason: 'no_predictions',
    pipeline_version: null,
    as_of: null,
    based_on_reels: 0,
    last_topic: null,
    backoff: false,
    row_observations: null,
    min_row_observations: null,
    hybrid_lambda: null,
    hybrid: null,
    models: [],
    evaluation: null,
  };
}

const MODELS = ['uniform', 'most_frequent', 'decayed_fingerprint', 'markov', 'hybrid'];

/** A prediction for a user with `reels` Reels scored; complete only with `complete: true`. */
export function makePrediction(
  options: { reels?: number; backoff?: boolean; complete?: boolean; beats?: boolean } = {},
) {
  const reels = options.reels ?? 300;
  const complete = options.complete ?? false;
  const topic = (id: number, label: string, probability: number) => ({
    topic_id: id,
    slug: label.toLowerCase().replace(/\W+/g, '-'),
    label,
    probability,
  });
  const hybrid = {
    model: 'hybrid',
    name: 'Fingerprint and what follows the last topic, combined',
    top: [
      topic(1, 'Cricket', 0.41),
      topic(2, 'Tech and coding', 0.27),
      topic(3, 'Food', 0.12),
      topic(4, 'Travel', 0.07),
      topic(5, 'Music', 0.05),
    ],
    rest_probability: 0.08,
  };
  const figures = (top1: number, n: number) => ({ n, top1, top3: 0.82, log_loss: 1.45 });
  const comparison = (verdict: string) => ({
    log_loss_difference: { estimate: -0.04, low: -0.09, high: 0.01 },
    top1_difference: { estimate: 0.01, low: -0.01, high: 0.03 },
    log_loss_verdict: verdict,
  });
  return {
    label: 'Predicted next observed topic',
    note: 'A statistical guess at the topic of the next Reel the feed shows you, from your own history. It is not a prediction of what you like.',
    available: true,
    reason: null,
    pipeline_version: 'pr1-test',
    as_of: '2026-10-09T10:00:00Z',
    based_on_reels: reels,
    last_topic: topic(2, 'Tech and coding', 1),
    backoff: options.backoff ?? false,
    row_observations: options.backoff ? 7 : 63,
    min_row_observations: 20,
    hybrid_lambda: 0.3,
    hybrid,
    models: MODELS.map((m) => ({ ...hybrid, model: m })),
    evaluation: {
      scored_reels: reels,
      required_reels: 1500,
      complete,
      status: complete ? 'complete' : `not complete: ${reels} of 1500 Reels scored`,
      rolling_window: 100,
      models: Object.fromEntries(
        MODELS.map((m, i) => [m, { all_time: figures(0.3 + i * 0.02, reels), rolling: figures(0.31 + i * 0.02, 100) }]),
      ),
      comparisons: complete
        ? {
            uniform: comparison('hybrid_lower'),
            most_frequent: comparison(options.beats ? 'hybrid_lower' : 'no_clear_difference'),
            decayed_fingerprint: comparison(options.beats ? 'hybrid_lower' : 'no_clear_difference'),
            markov: comparison('no_clear_difference'),
          }
        : null,
      hybrid_beats_required_baselines: complete ? (options.beats ?? false) : null,
    },
  };
}

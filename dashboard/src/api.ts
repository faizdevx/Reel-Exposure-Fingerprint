export interface Health {
  status: string;
}

export type FingerprintKind = 'exposure' | 'engagement';

export interface TopicShare {
  topic_id: number;
  slug: string;
  label: string;
  /** Fraction of the whole fingerprint, 0 to 1. All shares of one response sum to 1. */
  share: number;
}

export interface Fingerprint {
  kind: FingerprintKind;
  taken_at: string | null;
  n_reels: number;
  pipeline_version: string | null;
  session_id: string | null;
  topics: TopicShare[];
}

export interface EvidenceReel {
  event_id: string;
  session_id: string;
  reel_shortcode: string;
  reel_url: string;
  caption: string | null;
  caption_truncated: boolean;
  observed_at: string;
  dwell_ms: number | null;
  video_duration_ms: number | null;
  /** The Reel's fused score for the topic, 0 to 1. */
  topic_score: number;
  p_text: number | null;
  p_visual: number | null;
  gain: number;
  /** Recency factor, 0 to 1. */
  recency: number;
  /** topic_score x gain x recency. */
  contribution: number;
  /** This Reel's part of the topic's weight, 0 to 1. */
  share_of_topic: number;
}

export type EvidenceReason = 'no_fingerprint' | 'topic_not_in_fingerprint' | 'snapshot_without_provenance';

export interface TopicEvidence {
  topic: { id: number; slug: string; label: string };
  kind: FingerprintKind;
  evidence_available: boolean;
  reason: EvidenceReason | null;
  fingerprint: {
    taken_at: string;
    pipeline_version: string;
    n_reels: number;
    half_life_days: number;
    topic_weight: number;
    total_weight: number;
    topic_share: number;
  } | null;
  explanation: {
    gain_rule: string;
    formula: string;
    contributing_reels: number;
    contributions_sum: number;
    coverage: number;
  } | null;
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  items: EvidenceReel[];
}

export type HistoryBucket = 'day' | 'session';

export interface HistoryTopic {
  topic_id: number;
  slug: string;
  label: string;
}

export interface HistoryPoint {
  taken_at: string;
  session_id: string | null;
  /** Reels counted so far, in total. */
  n_reels: number;
  /** Reels counted inside this day or session alone; null when the snapshot has no such figure. */
  reels_in_bucket: number | null;
  /** Topic id (as text) to share, 0 to 1. Sums to 1. */
  shares: Record<string, number>;
}

export interface FingerprintHistory {
  kind: FingerprintKind;
  bucket: HistoryBucket;
  pipeline_version: string | null;
  topics: HistoryTopic[];
  /** Oldest first. */
  points: HistoryPoint[];
}

export interface SessionSummary {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  stop_reason: string | null;
  /** Null until the session has been processed into a snapshot. */
  n_reels: number | null;
}

export interface User {
  id: string;
  email: string;
}

/** A non-2xx answer. `status` 401 means "not logged in". */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`request failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
  }
}

// The server requires this header on cookie-authenticated state changes (CSRF, ADR-004).
const CSRF_HEADERS = { 'X-Requested-With': 'ref-dashboard' };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...init.headers },
  });
  if (!response.ok) throw new ApiError(response.status);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Liveness probe. */
export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const init: RequestInit = signal ? { signal } : {};
  return request<Health>('/healthz', init);
}

export function login(email: string, password: string): Promise<User> {
  return request<User>('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<void> {
  await request<null>('/api/v1/auth/logout', { method: 'POST', headers: CSRF_HEADERS });
}

export function fetchFingerprint(kind: FingerprintKind, signal?: AbortSignal): Promise<Fingerprint> {
  const init: RequestInit = signal ? { signal } : {};
  return request<Fingerprint>(`/api/v1/fingerprint/current?kind=${kind}`, init);
}

export function fetchHistory(
  kind: FingerprintKind,
  bucket: HistoryBucket,
  limit: number,
  signal?: AbortSignal,
): Promise<FingerprintHistory> {
  const init: RequestInit = signal ? { signal } : {};
  return request<FingerprintHistory>(
    `/api/v1/fingerprint/history?kind=${kind}&bucket=${bucket}&limit=${limit}`,
    init,
  );
}

export function fetchEvidence(
  kind: FingerprintKind,
  topicId: number,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<TopicEvidence> {
  const init: RequestInit = signal ? { signal } : {};
  return request<TopicEvidence>(
    `/api/v1/topics/${topicId}/evidence?kind=${kind}&limit=${limit}&offset=${offset}`,
    init,
  );
}

/** What a deletion did, and what its verification still found. All `remaining` zero means done. */
export interface DeletionReport {
  scope: 'session' | 'all';
  existed: boolean;
  deleted: Record<string, number>;
  remaining: Record<string, number>;
  frame_files_deleted: number;
  fingerprints_rebuilt: number;
  fingerprints_dropped: number;
  verified: boolean;
}

/** The server refused to call a deletion done: `remaining` says what is left. Calling again finishes it. */
export class DeletionIncompleteError extends Error {
  readonly report: DeletionReport;

  constructor(report: DeletionReport) {
    super('deletion incomplete');
    this.name = 'DeletionIncompleteError';
    this.report = report;
  }
}

async function deletion(path: string): Promise<DeletionReport> {
  const response = await fetch(path, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...CSRF_HEADERS },
  });
  if (response.status === 500) {
    const body = (await response.json().catch(() => null)) as DeletionReport | null;
    if (body && typeof body === 'object' && 'remaining' in body) throw new DeletionIncompleteError(body);
  }
  if (!response.ok) throw new ApiError(response.status);
  return (await response.json()) as DeletionReport;
}

export function deleteSession(id: string): Promise<DeletionReport> {
  return deletion(`/api/v1/sessions/${id}`);
}

export function deleteAllData(): Promise<DeletionReport> {
  return deletion('/api/v1/me/data');
}

export interface SurveyTopic {
  topic_id: number;
  slug: string;
  label: string;
}

export interface SurveyState {
  status: 'pending' | 'submitted' | 'skipped';
  completed_at: string | null;
  topics: SurveyTopic[];
  /** Topic id (as text) to rating 1 to 5, once submitted. */
  ratings: Record<string, number>;
}

export function fetchSelfReport(signal?: AbortSignal): Promise<SurveyState> {
  const init: RequestInit = signal ? { signal } : {};
  return request<SurveyState>('/api/v1/self-report', init);
}

export function submitSelfReport(ratings: Record<string, number>): Promise<SurveyState> {
  return request<SurveyState>('/api/v1/self-report', {
    method: 'PUT',
    headers: { ...CSRF_HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify({ ratings }),
  });
}

export function skipSelfReport(): Promise<SurveyState> {
  return request<SurveyState>('/api/v1/self-report/skip', { method: 'POST', headers: CSRF_HEADERS });
}

export interface PredictedTopic {
  topic_id: number;
  slug: string;
  label: string;
  probability: number;
}

export interface ModelPrediction {
  model: string;
  name: string;
  top: PredictedTopic[];
  rest_probability: number;
}

export interface ModelFigures {
  n: number;
  top1: number | null;
  top3: number | null;
  log_loss: number | null;
}

export interface Interval {
  estimate: number | null;
  low: number | null;
  high: number | null;
}

export interface PredictionEvaluation {
  scored_reels: number;
  required_reels: number;
  complete: boolean;
  status: string;
  rolling_window: number;
  models: Record<string, { all_time: ModelFigures; rolling: ModelFigures }>;
  /** Null until `complete`. Keyed by the model the hybrid is compared with. */
  comparisons: Record<
    string,
    { log_loss_difference: Interval; top1_difference: Interval; log_loss_verdict: string }
  > | null;
  hybrid_beats_required_baselines: boolean | null;
}

export interface NextPrediction {
  /** Always "Predicted next observed topic". */
  label: string;
  note: string;
  available: boolean;
  reason: string | null;
  pipeline_version: string | null;
  as_of: string | null;
  based_on_reels: number;
  last_topic: PredictedTopic | null;
  backoff: boolean;
  row_observations: number | null;
  min_row_observations: number | null;
  hybrid_lambda: number | null;
  hybrid: ModelPrediction | null;
  models: ModelPrediction[];
  evaluation: PredictionEvaluation | null;
}

export function fetchNextPrediction(signal?: AbortSignal): Promise<NextPrediction> {
  const init: RequestInit = signal ? { signal } : {};
  return request<NextPrediction>('/api/v1/predictions/next', init);
}

export function fetchSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  const init: RequestInit = signal ? { signal } : {};
  return request<SessionSummary[]>('/api/v1/sessions', init);
}

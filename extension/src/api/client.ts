/**
 * The only module that talks to the REF server. Every request carries the extension token as a
 * Bearer header; the token is never logged, and without one no request is made.
 */

export type StopReason = 'user' | 'left_surface' | 'tab_closed' | 'time_cap' | 'browser_restart';

export interface ConsentDto {
  policy_version: string;
  granted_at: string;
  revoked_at: string | null;
}

export interface SessionDto {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  stop_reason: string | null;
}

export interface EventUpload {
  event_id: string;
  body: Record<string, unknown>;
}

/** Server verdict on one uploaded event (POST /events). */
export interface EventResult {
  event_id: string;
  status: 'stored' | 'duplicate' | 'rejected';
  reason: string | null;
}

const RESULT_STATUSES: readonly string[] = ['stored', 'duplicate', 'rejected'];

/** The reply must name only events that were sent, each with a known status. */
function parseEventResults(value: unknown, sent: ReadonlySet<string>): EventResult[] | null {
  if (typeof value !== 'object' || value === null) return null;
  const list = (value as { results?: unknown }).results;
  if (!Array.isArray(list)) return null;
  const out: EventResult[] = [];
  for (const item of list) {
    if (typeof item !== 'object' || item === null) return null;
    const r = item as Record<string, unknown>;
    const { event_id: id, status, reason } = r;
    if (typeof id !== 'string' || !sent.has(id)) return null;
    if (typeof status !== 'string' || !RESULT_STATUSES.includes(status)) return null;
    if (!(reason === null || reason === undefined || typeof reason === 'string')) return null;
    out.push({ event_id: id, status: status as EventResult['status'], reason: reason ?? null });
  }
  return out;
}

/** `Retry-After` in whole seconds (the only form this server sends) as milliseconds, else null. */
export function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d{1,6}$/.test(value.trim())) return null;
  return Number(value.trim()) * 1000;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    /** Server `detail` when it is a string (for example "consent_required"), else a local code. */
    readonly code: string,
    /** From a `Retry-After` header (429 or 503): how long the server asked us to wait. */
    readonly retryAfterMs: number | null = null,
  ) {
    super(`API error ${status}: ${code}`);
    this.name = 'ApiError';
  }
}

export interface ApiClient {
  putConsent(policyVersion: string): Promise<ConsentDto>;
  deleteConsent(): Promise<void>;
  startSession(): Promise<SessionDto>;
  stopSession(sessionId: string, reason: StopReason): Promise<SessionDto>;
  /**
   * POST /events. Resolves with one result per event the server looked at; an event missing from
   * the results was not handled and must be sent again. Rejects with ApiError on any HTTP error,
   * and with code `bad_response` when the reply cannot be trusted.
   */
  postEvents(events: readonly EventUpload[]): Promise<EventResult[]>;
}

export interface ApiClientOptions {
  baseUrl: string;
  getToken: () => Promise<string | null>;
  fetchFn?: typeof fetch;
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const doFetch: typeof fetch = options.fetchFn ?? ((input, init) => fetch(input, init));

  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = await options.getToken();
    if (token === null) throw new ApiError(0, 'no_token');

    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response: Response;
    try {
      response = await doFetch(`${options.baseUrl}/api/v1${path}`, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (!response.ok) {
      let code = `http_${response.status}`;
      try {
        const parsed = (await response.json()) as { detail?: unknown };
        if (typeof parsed.detail === 'string') code = parsed.detail;
      } catch {
        // keep the generic code
      }
      throw new ApiError(response.status, code, parseRetryAfter(response.headers.get('Retry-After')));
    }
    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError(response.status, 'bad_response');
    }
  }

  return {
    putConsent: (policyVersion) =>
      request<ConsentDto>('PUT', '/consent', { policy_version: policyVersion }),
    deleteConsent: async () => {
      await request<null>('DELETE', '/consent');
    },
    startSession: () => request<SessionDto>('POST', '/sessions'),
    stopSession: (sessionId, reason) =>
      request<SessionDto>('PATCH', `/sessions/${encodeURIComponent(sessionId)}`, {
        status: 'stopped',
        stop_reason: reason,
      }),
    postEvents: async (events) => {
      const reply = await request<unknown>('POST', '/events', {
        events: events.map((e) => ({ ...e.body, event_id: e.event_id })),
      });
      const results = parseEventResults(reply, new Set(events.map((e) => e.event_id)));
      if (results === null) throw new ApiError(200, 'bad_response');
      return results;
    },
  };
}

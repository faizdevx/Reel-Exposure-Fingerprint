import { isExposureBody, type ExposureBody } from './exposure';

/** Typed messages between popup and service worker. Extend as features land. */
export type Request =
  | { type: 'getStatus' }
  | { type: 'grantConsent' }
  | { type: 'revokeConsent' }
  | { type: 'startSession' }
  | { type: 'stopSession' };

const REQUEST_TYPES: readonly string[] = [
  'getStatus',
  'grantConsent',
  'revokeConsent',
  'startSession',
  'stopSession',
];

export interface StatusResponse {
  /** Consent is recorded for the current policy text. This is the extension's collection gate. */
  consent: boolean;
  /** The server's grant time for the stored consent, or null. */
  consentGrantedAt: string | null;
  /** A revocation is done locally but the server has not confirmed it yet. */
  revocationPending: boolean;
  /** A monitoring session is running (and consent is active). */
  monitoring: boolean;
  /** An extension token is stored. The token itself is never part of any message. */
  hasToken: boolean;
}

export type ActionResponse = { ok: true } | { ok: false; error: string };

export function isRequest(value: unknown): value is Request {
  if (typeof value !== 'object' || value === null) return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === 'string' && REQUEST_TYPES.includes(type);
}

/**
 * Messages from the content script (running on instagram.com) to the service worker.
 * The content script owns no storage: everything it learns goes through these messages.
 */
export type ContentRequest =
  /** "Should I show the marker?" Sent once when the script loads on a Reels page. */
  | { type: 'contentHello' }
  /**
   * One observed Reel. `body` is the event without `session_id`: the service worker adds that.
   * `flush` is set when the page is going away (pagehide): upload now instead of on the timer.
   */
  | { type: 'reelEvent'; event_id: string; body: ExposureBody; flush?: boolean };

const CONTENT_REQUEST_TYPES: readonly string[] = ['contentHello', 'reelEvent'];

export interface ContentState {
  /** Monitoring is on for this tab and it is on the Reels surface. */
  active: boolean;
}

export interface EventResponse {
  accepted: boolean;
}

/** Service worker to content script: monitoring turned on or off for this tab. */
export interface ContentPush {
  type: 'monitoringState';
  active: boolean;
}

export const MAX_EVENT_BYTES = 256 * 1024; // spec §8 ingest rule: body over 256 KB is rejected
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isContentRequest(value: unknown): value is ContentRequest {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  if (typeof v['type'] !== 'string' || !CONTENT_REQUEST_TYPES.includes(v['type'])) return false;
  if (v['type'] === 'contentHello') return true;
  const body = v['body'];
  return (
    (v['flush'] === undefined || typeof v['flush'] === 'boolean') &&
    typeof v['event_id'] === 'string' &&
    UUID.test(v['event_id']) &&
    isExposureBody(body) &&
    JSON.stringify(body).length <= MAX_EVENT_BYTES
  );
}

export function isContentPush(value: unknown): value is ContentPush {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as ContentPush).type === 'monitoringState' &&
    typeof (value as ContentPush).active === 'boolean'
  );
}

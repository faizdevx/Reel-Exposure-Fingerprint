/**
 * Upload scheduler (spec F8, N5, §5.1). Owned by the service worker; the content script never
 * uploads.
 *
 * Events wait in the IndexedDB queue and leave in batches of at most 50 events and at most
 * BATCH_BYTES of JSON. A queued event is sent 15 seconds after the first one is queued, or at once
 * when ten are waiting. Two timers carry that: an in-memory 15 s timer (fast, but lost if the
 * worker is suspended) and a chrome.alarms alarm, which survives suspension but which Chrome fires
 * no sooner than 30 s out. The due time is persisted, so whichever fires first sends.
 *
 * An event leaves the queue only when the server has answered for it: `stored`, `duplicate`
 * (already stored by an earlier attempt whose reply was lost) or `rejected` (the server will never
 * accept it, for example its session was stopped; counted, not retried). Anything else, such as a
 * network error, a 5xx, a 401 or a reply that does not mention the event, keeps it queued and
 * backs off exponentially (30 s, 1 min, 2 min ... 15 min). The server dedupes by event_id, so
 * resending is always safe: nothing is lost and nothing is stored twice.
 *
 * A batch the server refuses as a whole as malformed or too large (400, 413, 422) is split in
 * half and retried, down to single events, so one bad event cannot block the queue. A single
 * event refused that way is dropped and counted. A 403 `consent_required` clears local consent and
 * deletes the queue (spec F1). A 429 (rate limited) or 503 keeps the queue and waits at least as
 * long as its `Retry-After` says (audit H-1).
 */

import { ApiError, type EventResult } from '../api/client';
import { clearConsent, isConsentActive } from '../consent/store';
import type { Deps } from '../runtime';
import { countEvents, removeEvents, type QueueDb, type QueuedEvent } from '../queue/db';

export const UPLOAD_ALARM = 'ref-upload';
export const UPLOAD_INTERVAL_MS = 15_000;
/** Chrome does not fire an alarm sooner than this; shorter waits also need the in-memory timer. */
export const ALARM_MIN_MS = 30_000;
export const MAX_BACKOFF_MS = 15 * 60_000;
export const BATCH_SIZE = 50;
/** Stay under the server's 256 KB body limit with room for the envelope. */
export const BATCH_BYTES = 240 * 1024;
export const FLUSH_AT = 10;

const DUE_KEY = 'uploadDueAt';
const FAILURES_KEY = 'uploadFailures';
const DROPPED_KEY = 'uploadDropped';
const MAX_REQUESTS_PER_RUN = 50;
const PERMANENT_HTTP = new Set([400, 413, 422]);

export function backoffMs(failures: number): number {
  return Math.min(UPLOAD_INTERVAL_MS * 2 ** Math.max(0, failures), MAX_BACKOFF_MS);
}

export type FlushResult = 'empty' | 'sent' | 'no_consent' | 'failed';

/** Events the server refused for good, by reason. IDs and counts only, never content. */
export type DroppedCounts = Record<string, number>;

async function readNumber(deps: Deps, key: string): Promise<number | null> {
  const value = (await deps.store.get([key]))[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export async function readDropped(deps: Deps): Promise<DroppedCounts> {
  const value = (await deps.store.get([DROPPED_KEY]))[DROPPED_KEY];
  if (typeof value !== 'object' || value === null) return {};
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, number] => typeof entry[1] === 'number'),
  );
}

async function countDropped(deps: Deps, reasons: readonly string[]): Promise<void> {
  if (reasons.length === 0) return;
  const counts = await readDropped(deps);
  for (const reason of reasons) counts[reason] = (counts[reason] ?? 0) + 1;
  await deps.store.set({ [DROPPED_KEY]: counts });
}

async function setDue(deps: Deps, delayMs: number): Promise<void> {
  const due = deps.now() + delayMs;
  await deps.store.set({ [DUE_KEY]: due });
  await deps.alarms.create(UPLOAD_ALARM, due);
  if (delayMs < ALARM_MIN_MS) deps.wakeAfter(delayMs);
}

/** Forget scheduling state: nothing is waiting to be sent. */
export async function clearUploadState(deps: Deps): Promise<void> {
  await deps.store.remove([DUE_KEY, FAILURES_KEY]);
  await deps.alarms.clear(UPLOAD_ALARM);
}

const sizeOf = (e: QueuedEvent): number => JSON.stringify({ ...e.body, event_id: e.event_id }).length;

/** The oldest events first: at most `limit` events and BATCH_BYTES, but always at least one. */
async function peek(db: QueueDb, limit: number): Promise<QueuedEvent[]> {
  const all = await db.getAll('events');
  all.sort((a, b) => a.enqueued_at.localeCompare(b.enqueued_at) || a.event_id.localeCompare(b.event_id));
  const batch: QueuedEvent[] = [];
  let bytes = 0;
  for (const e of all.slice(0, limit)) {
    bytes += sizeOf(e) + 1;
    if (batch.length > 0 && bytes > BATCH_BYTES) break;
    batch.push(e);
  }
  return batch;
}

async function waitingCount(deps: Deps): Promise<number> {
  const db = await deps.openQueue();
  try {
    return await countEvents(db);
  } finally {
    db.close();
  }
}

/**
 * Call after an event was queued: send now when enough are waiting, else make sure one is due.
 * While a backoff is pending (the server could not be reached) new events wait for it: one request
 * per new event would hammer a dead connection and push the backoff to its cap within a minute.
 */
export async function onEventQueued(deps: Deps): Promise<void> {
  const due = await readNumber(deps, DUE_KEY);
  const backingOff =
    due !== null && due > deps.now() && (await readNumber(deps, FAILURES_KEY)) !== null;
  if (!backingOff && (await waitingCount(deps)) >= FLUSH_AT) {
    await flushQueue(deps);
    return;
  }
  if (due === null || due <= deps.now()) await setDue(deps, UPLOAD_INTERVAL_MS);
}

/** The in-memory timer or the alarm fired: send if something is due. */
export async function onUploadDue(deps: Deps): Promise<FlushResult | null> {
  const due = await readNumber(deps, DUE_KEY);
  if (due !== null && due > deps.now()) return null; // a newer schedule (a backoff) is pending
  return flushQueue(deps);
}

// One flush at a time per service worker: a second caller waits for the run already going, which
// keeps sending until the queue is empty. Two runs could not lose or duplicate anything (the
// server dedupes), but they would send the same batch twice.
const running = new WeakMap<Deps, Promise<FlushResult>>();

/** Send queued events until the queue is empty or the server cannot be reached. */
export function flushQueue(deps: Deps): Promise<FlushResult> {
  const current = running.get(deps);
  if (current !== undefined) return current;
  const run = flush(deps).finally(() => running.delete(deps));
  running.set(deps, run);
  return run;
}

async function flush(deps: Deps): Promise<FlushResult> {
  if (!(await isConsentActive(deps.store))) {
    await clearUploadState(deps);
    return 'no_consent';
  }

  const db = await deps.openQueue();
  try {
    let sent = false;
    let limit = BATCH_SIZE;
    for (let i = 0; i < MAX_REQUESTS_PER_RUN; i += 1) {
      const batch = await peek(db, limit);
      if (batch.length === 0) {
        await clearUploadState(deps);
        return sent ? 'sent' : 'empty';
      }
      // Consent can be revoked between batches; the revoke path deletes the queue too.
      if (!(await isConsentActive(deps.store))) {
        await clearUploadState(deps);
        return 'no_consent';
      }

      let results: EventResult[];
      try {
        results = await deps.api.postEvents(batch);
      } catch (error) {
        if (error instanceof ApiError && PERMANENT_HTTP.has(error.status)) {
          if (batch.length > 1) {
            limit = Math.ceil(batch.length / 2); // find the event the server cannot take
            continue;
          }
          await removeEvents(db, batch.map((e) => e.event_id)); // the one event that is left
          await countDropped(deps, [`http_${error.status}`]);
          limit = BATCH_SIZE;
          continue;
        }
        return await failed(deps, error);
      }

      // Only events the server answered for leave the queue.
      await removeEvents(db, results.map((r) => r.event_id));
      await countDropped(
        deps,
        results.filter((r) => r.status === 'rejected').map((r) => r.reason ?? 'rejected'),
      );
      if (new Set(results.map((r) => r.event_id)).size < batch.length) {
        return await failed(deps, new ApiError(200, 'partial')); // the rest is resent later
      }
      await deps.store.remove([FAILURES_KEY]);
      sent = true;
      limit = BATCH_SIZE;
    }
    await setDue(deps, UPLOAD_INTERVAL_MS); // more waiting than one run sends
    return 'sent';
  } finally {
    db.close();
  }
}

async function failed(deps: Deps, error: unknown): Promise<FlushResult> {
  if (error instanceof ApiError && error.code === 'consent_required') {
    // The server says there is no consent. Believe it: stop and delete what is queued.
    await clearConsent(deps.store);
    await deps.purgeQueue();
    await clearUploadState(deps);
    return 'no_consent';
  }
  const failures = ((await readNumber(deps, FAILURES_KEY)) ?? 0) + 1;
  await deps.store.set({ [FAILURES_KEY]: failures });
  // A server that says how long to wait (429, 503) is believed when it asks for more than our own
  // backoff; never less, so a tiny Retry-After cannot make us hammer it. The queue is kept.
  const asked = error instanceof ApiError ? (error.retryAfterMs ?? 0) : 0;
  await setDue(deps, Math.min(Math.max(backoffMs(failures), asked), MAX_BACKOFF_MS));
  return 'failed';
}

/** Browser or worker start: events from before still need sending. */
export async function resumeUpload(deps: Deps): Promise<FlushResult> {
  return (await waitingCount(deps)) > 0 ? flushQueue(deps) : 'empty';
}

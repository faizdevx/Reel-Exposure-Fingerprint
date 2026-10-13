import { beforeEach, describe, expect, it } from 'vitest';

import { handleAlarm } from '../src/background/handlers';
import { ApiError, type EventResult } from '../src/api/client';
import { revokeConsent } from '../src/consent/service';
import { isConsentActive } from '../src/consent/store';
import { countEvents, enqueueEvent } from '../src/queue/db';
import {
  BATCH_BYTES,
  BATCH_SIZE,
  FLUSH_AT,
  MAX_BACKOFF_MS,
  UPLOAD_ALARM,
  UPLOAD_INTERVAL_MS,
  backoffMs,
  flushQueue,
  onEventQueued,
  onUploadDue,
  readDropped,
  resumeUpload,
} from '../src/upload/scheduler';
import { makeEnv, type Env } from './helpers';

let env: Env;
beforeEach(() => {
  env = makeEnv();
});

async function seed(n: number, start = 0) {
  const db = await env.deps.openQueue();
  for (let i = start; i < start + n; i += 1) {
    await enqueueEvent(db, {
      event_id: `e${String(i).padStart(4, '0')}`,
      enqueued_at: new Date(env.now() + i).toISOString(),
      body: { n: i },
    });
  }
  db.close();
}
async function waiting() {
  const db = await env.deps.openQueue();
  const n = await countEvents(db);
  db.close();
  return n;
}

describe('flushQueue', () => {
  it('sends nothing and makes no request when the queue is empty', async () => {
    await env.ready();
    expect(await flushQueue(env.deps)).toBe('empty');
    expect(env.api.postEvents).not.toHaveBeenCalled();
  });

  it('sends oldest first in batches of at most 50, and empties the queue', async () => {
    await env.ready();
    await seed(120);
    expect(await flushQueue(env.deps)).toBe('sent');
    const sizes = env.api.postEvents.mock.calls.map((c) => c[0].length);
    expect(sizes).toEqual([BATCH_SIZE, BATCH_SIZE, 20]);
    expect(env.api.postEvents.mock.calls[0]?.[0][0]?.event_id).toBe('e0000');
    expect(await waiting()).toBe(0);
    expect(env.alarms.has(UPLOAD_ALARM)).toBe(false);
  });

  it('loses nothing when the network drops, and resends the same events later', async () => {
    await env.ready();
    await seed(5);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(0, 'network'));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(5);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(1));

    env.advance(backoffMs(1));
    expect(await flushQueue(env.deps)).toBe('sent');
    expect(await waiting()).toBe(0);
    const ids = (i: number) => env.api.postEvents.mock.calls[i]?.[0].map((e) => e.event_id);
    expect(ids(1)).toEqual(ids(0));
  });

  it('sends nothing with no consent', async () => {
    env.token();
    await seed(3);
    expect(await flushQueue(env.deps)).toBe('no_consent');
    expect(env.api.postEvents).not.toHaveBeenCalled();
  });

  it('stops and deletes the queue when the server says consent is required', async () => {
    await env.ready();
    await seed(3);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(403, 'consent_required'));
    expect(await flushQueue(env.deps)).toBe('no_consent');
    expect(await isConsentActive(env.store)).toBe(false);
    expect(await waiting()).toBe(0);
  });

  it('keeps events when the token is rejected', async () => {
    await env.ready();
    await seed(2);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(401, 'http_401'));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(2);
  });
});

describe('backoff', () => {
  it('doubles from 30 s and caps at 15 minutes', () => {
    expect(backoffMs(0)).toBe(UPLOAD_INTERVAL_MS);
    expect(backoffMs(1)).toBe(30_000);
    expect(backoffMs(2)).toBe(60_000);
    expect(backoffMs(3)).toBe(120_000);
    expect(backoffMs(20)).toBe(MAX_BACKOFF_MS);
  });

  it('grows with repeated failures and resets after a success', async () => {
    await env.ready();
    await seed(1);
    env.api.postEvents.mockRejectedValue(new ApiError(0, 'network'));
    await flushQueue(env.deps);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(1));
    await flushQueue(env.deps);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(2));

    env.api.postEvents.mockImplementation((events) =>
      Promise.resolve(events.map((e) => ({ event_id: e.event_id, status: 'stored', reason: null }))),
    );
    await flushQueue(env.deps);
    await seed(1, 10);
    env.api.postEvents.mockRejectedValue(new ApiError(0, 'network'));
    await flushQueue(env.deps);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(1));
  });
});

describe('scheduling', () => {
  it('sets one alarm for a few events and does not push it back for the next', async () => {
    await env.ready();
    await seed(1);
    await onEventQueued(env.deps);
    const first = env.alarms.get(UPLOAD_ALARM);
    expect(first).toBe(env.now() + UPLOAD_INTERVAL_MS);
    env.advance(5_000);
    await seed(1, 1);
    await onEventQueued(env.deps);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(first);
    expect(env.api.postEvents).not.toHaveBeenCalled();
  });

  it(`sends at once when ${FLUSH_AT} events are waiting`, async () => {
    await env.ready();
    await seed(FLUSH_AT);
    await onEventQueued(env.deps);
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    expect(await waiting()).toBe(0);
  });

  it('the upload alarm triggers a flush', async () => {
    await env.ready();
    await seed(2);
    await handleAlarm(env.deps, UPLOAD_ALARM);
    expect(env.api.postEvents).toHaveBeenCalledOnce();
  });

  it('after a browser restart, resumes sending events left in the queue', async () => {
    await env.ready();
    await seed(4);
    await resumeUpload(env.deps);
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    expect(await waiting()).toBe(0);
  });

  it('keeps sending queued events after the session ends, while consent holds', async () => {
    await env.ready();
    await seed(2);
    expect(await flushQueue(env.deps)).toBe('sent'); // no session at all: only consent matters
  });
});

describe('revoking consent', () => {
  it('deletes the queue and cancels the upload alarm; later flushes send nothing', async () => {
    await env.ready();
    await seed(3);
    await onEventQueued(env.deps);
    expect(env.alarms.has(UPLOAD_ALARM)).toBe(true);
    await revokeConsent(env.deps);
    expect(await waiting()).toBe(0);
    expect(env.alarms.has(UPLOAD_ALARM)).toBe(false);
    await seed(1, 50); // even if something were left behind
    expect(await flushQueue(env.deps)).toBe('no_consent');
    expect(env.api.postEvents).not.toHaveBeenCalled();
  });
});

const stored = (ids: readonly string[]): EventResult[] =>
  ids.map((event_id) => ({ event_id, status: 'stored', reason: null }));
const sentIds = (call: number) =>
  env.api.postEvents.mock.calls[call]?.[0].map((e) => e.event_id) ?? [];

describe('per-event results', () => {
  it('removes stored, duplicate and rejected events; counts the rejected ones by reason', async () => {
    await env.ready();
    await seed(4);
    env.api.postEvents.mockResolvedValueOnce([
      { event_id: 'e0000', status: 'stored', reason: null },
      { event_id: 'e0001', status: 'duplicate', reason: null },
      { event_id: 'e0002', status: 'rejected', reason: 'session_inactive' },
      { event_id: 'e0003', status: 'rejected', reason: 'session_inactive' },
    ]);
    expect(await flushQueue(env.deps)).toBe('sent');
    expect(await waiting()).toBe(0);
    expect(await readDropped(env.deps)).toEqual({ session_inactive: 2 });
  });

  it('keeps events the reply does not mention and retries them with backoff', async () => {
    await env.ready();
    await seed(3);
    env.api.postEvents.mockResolvedValueOnce(stored(['e0001']));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(2);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(1));
    expect(await flushQueue(env.deps)).toBe('sent');
    expect(sentIds(1)).toEqual(['e0000', 'e0002']);
  });

  it('keeps everything when the reply cannot be read', async () => {
    await env.ready();
    await seed(2);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(200, 'bad_response'));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(2);
  });

  it.each([500, 502, 503, 404, 429])('keeps everything on HTTP %i and backs off', async (status) => {
    await env.ready();
    await seed(2);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(status, `http_${status}`));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(2);
    expect(await readDropped(env.deps)).toEqual({});
  });
});

describe('rate limiting (audit H-1)', () => {
  it('waits at least as long as the server asked, and keeps every event', async () => {
    await env.ready();
    await seed(3);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(429, 'rate_limited', 300_000));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(await waiting()).toBe(3);
    expect(await readDropped(env.deps)).toEqual({});
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + 300_000);
  });

  it('never waits less than its own backoff, however small the request', async () => {
    await env.ready();
    await seed(1);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(429, 'rate_limited', 1000));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + backoffMs(1));
  });

  it('caps an absurd request at the maximum backoff', async () => {
    await env.ready();
    await seed(1);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(503, 'busy', 999_000_000));
    expect(await flushQueue(env.deps)).toBe('failed');
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + MAX_BACKOFF_MS);
  });

  it('sends the same events again after the wait, with no duplicates lost or made', async () => {
    await env.ready();
    await seed(2);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(429, 'rate_limited', 60_000));
    expect(await flushQueue(env.deps)).toBe('failed');
    env.advance(60_000);
    expect(await onUploadDue(env.deps)).toBe('sent');
    expect(await waiting()).toBe(0);
  });
});

describe('a batch the server refuses as a whole', () => {
  it.each([400, 413, 422])('HTTP %i: splits the batch and drops only the bad event', async (status) => {
    await env.ready();
    await seed(8);
    env.api.postEvents.mockImplementation((events) =>
      events.some((e) => e.event_id === 'e0005')
        ? Promise.reject(new ApiError(status, `http_${status}`))
        : Promise.resolve(stored(events.map((e) => e.event_id))),
    );
    expect(await flushQueue(env.deps)).toBe('sent');
    expect(await waiting()).toBe(0);
    expect(await readDropped(env.deps)).toEqual({ [`http_${status}`]: 1 });
    const delivered = env.api.postEvents.mock.calls
      .flatMap((c, i) => (c[0].some((e) => e.event_id === 'e0005') ? [] : sentIds(i)))
      .sort();
    expect(delivered).toEqual(['e0000', 'e0001', 'e0002', 'e0003', 'e0004', 'e0006', 'e0007']);
  });
});

describe('batch size in bytes', () => {
  it('keeps each request under the server body limit', async () => {
    await env.ready();
    const db = await env.deps.openQueue();
    for (let i = 0; i < 20; i += 1) {
      await enqueueEvent(db, {
        event_id: `big${String(i).padStart(2, '0')}`,
        enqueued_at: new Date(env.now() + i).toISOString(),
        body: { caption: 'x'.repeat(30_000) },
      });
    }
    db.close();
    expect(await flushQueue(env.deps)).toBe('sent');
    for (const [batch] of env.api.postEvents.mock.calls) {
      expect(JSON.stringify({ events: batch.map((e) => ({ ...e.body, event_id: e.event_id })) }).length)
        .toBeLessThanOrEqual(BATCH_BYTES + 1024);
    }
    expect(env.api.postEvents.mock.calls.length).toBeGreaterThan(1);
    expect(await waiting()).toBe(0);
  });
});

describe('the 15 second schedule', () => {
  it('asks for an in-memory wake-up after 15 s, with the alarm as the backstop', async () => {
    await env.ready();
    await seed(1);
    await onEventQueued(env.deps);
    expect(env.wakes).toEqual([UPLOAD_INTERVAL_MS]);
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() + UPLOAD_INTERVAL_MS);
  });

  it('long backoffs rely on the alarm alone', async () => {
    await env.ready();
    await seed(1);
    env.api.postEvents.mockRejectedValue(new ApiError(0, 'network'));
    await flushQueue(env.deps); // 30 s: alarm only
    expect(env.wakes).toEqual([]);
  });

  it('a wake-up that comes before a pending backoff does not send early', async () => {
    await env.ready();
    await seed(1);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(0, 'network'));
    await flushQueue(env.deps);
    env.advance(UPLOAD_INTERVAL_MS);
    expect(await onUploadDue(env.deps)).toBeNull();
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    env.advance(backoffMs(1));
    expect(await onUploadDue(env.deps)).toBe('sent');
  });
});

describe('one flush at a time', () => {
  it('a second caller joins the running flush instead of sending the same batch again', async () => {
    await env.ready();
    await seed(3);
    const [a, b] = await Promise.all([flushQueue(env.deps), flushQueue(env.deps)]);
    expect([a, b]).toEqual(['sent', 'sent']);
    expect(env.api.postEvents).toHaveBeenCalledOnce();
  });
});

describe('during an outage', () => {
  it('new events wait for the pending backoff instead of each firing a request', async () => {
    await env.ready();
    await seed(FLUSH_AT);
    env.api.postEvents.mockRejectedValue(new ApiError(0, 'network'));
    await onEventQueued(env.deps); // ten waiting: one attempt, which fails
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    for (let i = 0; i < 20; i += 1) {
      env.advance(1_000);
      await seed(1, 100 + i);
      await onEventQueued(env.deps);
    }
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    expect(env.alarms.get(UPLOAD_ALARM)).toBe(env.now() - 20_000 + backoffMs(1));
  });
});

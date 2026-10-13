import { beforeEach, describe, expect, it } from 'vitest';

import { ApiError } from '../src/api/client';
import { routeMessage, type Sender } from '../src/background/handlers';
import { countEvents } from '../src/queue/db';
import { startSession } from '../src/session/controller';
import { isContentPush, isContentRequest } from '../src/shared/messages';
import { REELS_URL, makeEnv, validBody, type Env } from './helpers';

const OWN = 'ext-id';
const EVENT_ID = '3f1c2b7e-9a4d-4e0b-8c55-0d5b1b2a7c11';
const popup: Sender = { id: OWN, tabId: undefined, tabUrl: undefined };
const content = (tabId = 7, tabUrl = REELS_URL): Sender => ({ id: OWN, tabId, tabUrl });
const reelEvent = { type: 'reelEvent', event_id: EVENT_ID, body: validBody() };

let env: Env;
const route = (message: unknown, sender: Sender) => routeMessage(env.deps, OWN, message, sender);
const queued = async () => {
  const db = await env.deps.openQueue();
  try {
    return await countEvents(db);
  } finally {
    db.close();
  }
};

beforeEach(() => {
  env = makeEnv();
});

describe('sender separation', () => {
  it('ignores messages from another extension', () => {
    expect(route({ type: 'getStatus' }, { ...popup, id: 'other' })).toBeNull();
    expect(route({ type: 'contentHello' }, { ...content(), id: 'other' })).toBeNull();
    expect(route({ type: 'getStatus' }, { ...popup, id: undefined })).toBeNull();
  });

  it('never lets a content script drive consent, sessions or status', () => {
    for (const type of ['getStatus', 'grantConsent', 'revokeConsent', 'startSession', 'stopSession']) {
      expect(route({ type }, content())).toBeNull();
    }
    expect(env.api.putConsent).not.toHaveBeenCalled();
  });

  it('never lets the popup send content messages', () => {
    expect(route({ type: 'contentHello' }, popup)).toBeNull();
    expect(route(reelEvent, popup)).toBeNull();
  });

  it('ignores unknown and malformed messages', () => {
    for (const bad of [null, 'getStatus', 42, {}, { type: 'nope' }, { type: 7 }]) {
      expect(route(bad, popup)).toBeNull();
      expect(route(bad, content())).toBeNull();
    }
  });

  it('routes a valid popup request to the handler', async () => {
    const reply = await route({ type: 'getStatus' }, popup);
    expect(reply).toMatchObject({ consent: false, monitoring: false, hasToken: false });
  });
});

describe('contentHello', () => {
  it('says inactive before any session', async () => {
    await env.ready();
    expect(await route({ type: 'contentHello' }, content())).toEqual({ active: false });
  });

  it('says active for the monitored tab on the Reels surface', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route({ type: 'contentHello' }, content())).toEqual({ active: true });
  });

  it('says inactive for another tab', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route({ type: 'contentHello' }, content(8))).toEqual({ active: false });
  });

  it('says inactive when the sender URL is not the Reels surface', async () => {
    await env.ready();
    await startSession(env.deps);
    for (const url of [
      'https://www.instagram.com/',
      'https://www.instagram.com/p/abc/',
      'https://www.instagram.com/reel/abc/',
      'https://example.com/reels/abc/',
    ]) {
      expect(await route({ type: 'contentHello' }, content(7, url))).toEqual({ active: false });
    }
  });
});

describe('reelEvent', () => {
  it('queues the event in the service worker, with the session id added by the worker', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route(reelEvent, content())).toEqual({ accepted: true });

    const db = await env.deps.openQueue();
    const stored = await db.get('events', EVENT_ID);
    db.close();
    expect(stored?.body).toEqual({ ...validBody(), session_id: 'session-1' });
  });

  it('refuses an event that carries its own session_id', () => {
    const forged = { ...reelEvent, body: { ...validBody(), session_id: 'forged' } };
    expect(route(forged, content())).toBeNull();
  });

  it('accepts a finished viewing after the tab has navigated off the Reels surface', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route(reelEvent, content(7, 'https://www.instagram.com/'))).toEqual({ accepted: true });
  });

  it('is idempotent: the same event_id twice leaves one queued event', async () => {
    await env.ready();
    await startSession(env.deps);
    await route(reelEvent, content());
    await route(reelEvent, content());
    expect(await queued()).toBe(1);
  });

  it('is refused with no session or from another tab', async () => {
    await env.ready();
    expect(await route(reelEvent, content())).toEqual({ accepted: false });
    await startSession(env.deps);
    expect(await route(reelEvent, content(8))).toEqual({ accepted: false });
    expect(await queued()).toBe(0);
  });

  it('is refused once consent is gone', async () => {
    await env.ready();
    await startSession(env.deps);
    await route({ type: 'revokeConsent' }, popup);
    expect(await route(reelEvent, content())).toEqual({ accepted: false });
    expect(await queued()).toBe(0);
  });
});

describe('message shape guards', () => {
  it('accepts well-formed content requests only', () => {
    expect(isContentRequest({ type: 'contentHello' })).toBe(true);
    expect(isContentRequest(reelEvent)).toBe(true);
    expect(isContentRequest({ ...reelEvent, event_id: 'not-a-uuid' })).toBe(false);
    expect(isContentRequest({ ...reelEvent, body: null })).toBe(false);
    expect(isContentRequest({ ...reelEvent, body: [] })).toBe(false);
    expect(isContentRequest({ ...reelEvent, body: { reel_shortcode: 'x' } })).toBe(false);
    expect(
      isContentRequest({ ...reelEvent, body: validBody({ caption: { value: 'a'.repeat(256 * 1024), status: 'available' } }) }),
    ).toBe(false);
    expect(isContentRequest({ type: 'getStatus' })).toBe(false);
  });

  it('accepts only a boolean monitoringState push', () => {
    expect(isContentPush({ type: 'monitoringState', active: true })).toBe(true);
    expect(isContentPush({ type: 'monitoringState', active: 'yes' })).toBe(false);
    expect(isContentPush({ type: 'other', active: true })).toBe(false);
    expect(isContentPush(null)).toBe(false);
  });
});

describe('exposure body guard', () => {
  const ok = (over: Record<string, unknown>) =>
    isContentRequest({ type: 'reelEvent', event_id: EVENT_ID, body: { ...validBody(), ...over } });

  it('rejects wrong types, ranges and statuses', () => {
    expect(ok({})).toBe(true);
    expect(ok({ dwell_ms: -1 })).toBe(false);
    expect(ok({ dwell_ms: 1.5 })).toBe(false);
    expect(ok({ dwell_ms: 25 * 60 * 60 * 1000 })).toBe(false);
    expect(ok({ reel_shortcode: '' })).toBe(false);
    expect(ok({ observed_at: 'yesterday' })).toBe(false);
    expect(ok({ frame_count: 6 })).toBe(false);
    expect(ok({ adapter_version: '' })).toBe(false);
    expect(ok({ unexpected: 1 })).toBe(false);
  });

  it('a value must be null exactly when the status is unavailable', () => {
    expect(ok({ caption: { value: null, status: 'unavailable' } })).toBe(true);
    expect(ok({ caption: { value: 'x', status: 'unavailable' } })).toBe(false);
    expect(ok({ caption: { value: null, status: 'available' } })).toBe(false);
    expect(ok({ hashtags: { value: [1], status: 'available' } })).toBe(false);
    expect(ok({ video_duration_ms: null, video_duration_status: 'unavailable' })).toBe(true);
    expect(ok({ video_duration_ms: null, video_duration_status: 'available' })).toBe(false);
    expect(ok({ video_duration_ms: 5, video_duration_status: 'unavailable' })).toBe(false);
  });
});

describe('pagehide flush', () => {
  it('a reelEvent marked flush is uploaded at once, not on the 15 s timer', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route({ ...reelEvent, flush: true }, content())).toEqual({ accepted: true });
    expect(env.api.postEvents).toHaveBeenCalledOnce();
    expect(env.api.postEvents.mock.calls[0]?.[0][0]?.event_id).toBe(EVENT_ID);
    expect(await queued()).toBe(0);
  });

  it('an ordinary reelEvent waits for the timer', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await route(reelEvent, content())).toEqual({ accepted: true });
    expect(env.api.postEvents).not.toHaveBeenCalled();
    expect(env.wakes).toHaveLength(1);
    expect(await queued()).toBe(1);
  });

  it('offline at pagehide: the event stays queued for the next attempt', async () => {
    await env.ready();
    await startSession(env.deps);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(0, 'network'));
    expect(await route({ ...reelEvent, flush: true }, content())).toEqual({ accepted: true });
    expect(await queued()).toBe(1);
  });

  it('refuses a flush flag that is not a boolean', () => {
    expect(isContentRequest({ ...reelEvent, flush: 'yes' })).toBe(false);
    expect(isContentRequest({ ...reelEvent, flush: false })).toBe(true);
  });
});

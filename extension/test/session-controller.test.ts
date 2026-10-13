import { describe, expect, it } from 'vitest';

import { ApiError } from '../src/api/client';
import { clearConsent, isConsentActive } from '../src/consent/store';
import { enqueueEvent } from '../src/queue/db';
import {
  flushPendingStops,
  onAlarm,
  onStartup,
  onTabRemoved,
  onTabUpdated,
  startSession,
  stopSession,
} from '../src/session/controller';
import {
  CAP_ALARM,
  LEAVE_ALARM,
  RETRY_ALARM,
  readPendingStops,
  readSession,
} from '../src/session/state';
import { MINUTE, REELS_URL, START, makeEnv, type Env } from './helpers';

async function started(): Promise<Env> {
  const env = makeEnv();
  await env.ready();
  expect(await startSession(env.deps)).toEqual({ ok: true });
  return env;
}

describe('startSession', () => {
  it('starts, remembers the session and arms the 60 minute alarm', async () => {
    const env = await started();
    expect(env.api.startSession).toHaveBeenCalledTimes(1);
    expect(await readSession(env.store)).toEqual({
      sessionId: 'session-1',
      tabId: 7,
      startedAt: START,
      leftSurfaceAt: null,
    });
    expect(env.alarms.get(CAP_ALARM)).toBe(START + 60 * MINUTE);
    expect(env.badge).toEqual([true]);
  });

  it('uses the server start time for the cap', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.startSession.mockResolvedValueOnce({
      id: 's2',
      started_at: new Date(START - 5 * MINUTE).toISOString(),
      ended_at: null,
      status: 'active',
      stop_reason: null,
    });
    await startSession(env.deps);
    expect(env.alarms.get(CAP_ALARM)).toBe(START + 55 * MINUTE);
  });

  it.each([
    ['consent', async (env: Env) => env.token(), 'no_consent'],
    ['token', async (env: Env) => env.consent(), 'no_token'],
  ])('refuses without %s and makes no request', async (_what, arrange, error) => {
    const env = makeEnv();
    await arrange(env);
    expect(await startSession(env.deps)).toEqual({ ok: false, error });
    expect(env.api.startSession).not.toHaveBeenCalled();
    expect(await readSession(env.store)).toBeNull();
    expect(env.badge).toEqual([]);
  });

  it.each([
    ['another site', { id: 7, url: 'https://example.test/' }],
    ['the Instagram home page', { id: 7, url: 'https://www.instagram.com/' }],
    ['a hidden URL', { id: 7, url: undefined }],
    ['no tab at all', { id: undefined, url: REELS_URL }],
  ])('refuses when the active tab is %s', async (_what, tab) => {
    const env = makeEnv();
    await env.ready();
    env.setActiveTab(tab);
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'not_on_reels' });
    expect(env.api.startSession).not.toHaveBeenCalled();
  });

  it('refuses a second session', async () => {
    const env = await started();
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'already_active' });
    expect(env.api.startSession).toHaveBeenCalledTimes(1);
  });

  it('drops local consent when the server says it has none', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.startSession.mockRejectedValueOnce(new ApiError(403, 'consent_required'));
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'consent_required' });
    expect(await isConsentActive(env.store)).toBe(false);
    expect(await readSession(env.store)).toBeNull();
  });

  it('reports a server that cannot be reached', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.startSession.mockRejectedValueOnce(new ApiError(0, 'network'));
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'network' });
    expect(await readSession(env.store)).toBeNull();
    expect(env.alarms.size).toBe(0);
  });
});

describe('stopSession', () => {
  it('stops locally, clears alarms and badge, and reports the reason', async () => {
    const env = await started();
    expect(await stopSession(env.deps, 'user')).toEqual({ ok: true });
    expect(await readSession(env.store)).toBeNull();
    expect(env.alarms.size).toBe(0);
    expect(env.badge.at(-1)).toBe(false);
    expect(env.api.stopSession).toHaveBeenCalledExactlyOnceWith('session-1', 'user');
  });

  it('says so when nothing is running', async () => {
    const env = makeEnv();
    expect(await stopSession(env.deps, 'user')).toEqual({ ok: false, error: 'not_active' });
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });

  it('is stopped locally even if the server cannot be told, and retries later', async () => {
    const env = await started();
    env.api.stopSession.mockRejectedValueOnce(new ApiError(0, 'network'));

    expect(await stopSession(env.deps, 'user')).toEqual({ ok: true });
    expect(await readSession(env.store)).toBeNull(); // collection already ended
    expect(await readPendingStops(env.store)).toEqual([{ sessionId: 'session-1', reason: 'user' }]);
    expect(env.alarms.has(RETRY_ALARM)).toBe(true);

    await flushPendingStops(env.deps);
    expect(await readPendingStops(env.store)).toEqual([]);
    expect(env.api.stopSession).toHaveBeenCalledTimes(2);
  });

  it('drops a stop the server will never accept', async () => {
    const env = await started();
    env.api.stopSession.mockRejectedValueOnce(new ApiError(404, 'Session not found'));
    await stopSession(env.deps, 'user');
    expect(await readPendingStops(env.store)).toEqual([]);
    expect(env.alarms.has(RETRY_ALARM)).toBe(false);
  });
});

describe('60 minute cap', () => {
  it('stops with time_cap when the alarm fires', async () => {
    const env = await started();
    env.advance(60 * MINUTE);
    await onAlarm(env.deps, CAP_ALARM);
    expect(await readSession(env.store)).toBeNull();
    expect(env.api.stopSession).toHaveBeenCalledWith('session-1', 'time_cap');
  });

  it('does not stop early if an alarm fires before the cap', async () => {
    const env = await started();
    env.advance(59 * MINUTE);
    await onAlarm(env.deps, CAP_ALARM);
    expect(await readSession(env.store)).not.toBeNull();
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });
});

describe('leaving the Reels surface', () => {
  const away = 'https://example.test/';

  it('starts a 60 second countdown when the tab leaves Reels', async () => {
    const env = await started();
    env.advance(5 * MINUTE);
    env.setTabUrl(7, away);
    await onTabUpdated(env.deps, 7);
    expect((await readSession(env.store))?.leftSurfaceAt).toBe(START + 5 * MINUTE);
    expect(env.alarms.get(LEAVE_ALARM)).toBe(START + 5 * MINUTE + 60_000);
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });

  it('stops with left_surface when still away after 60 seconds', async () => {
    const env = await started();
    env.setTabUrl(7, away);
    await onTabUpdated(env.deps, 7);
    env.advance(60_000);
    await onAlarm(env.deps, LEAVE_ALARM);
    expect(await readSession(env.store)).toBeNull();
    expect(env.api.stopSession).toHaveBeenCalledWith('session-1', 'left_surface');
  });

  it('does not stop at 59 seconds', async () => {
    const env = await started();
    env.setTabUrl(7, away);
    await onTabUpdated(env.deps, 7);
    env.advance(59_000);
    await onAlarm(env.deps, LEAVE_ALARM);
    expect(await readSession(env.store)).not.toBeNull();
  });

  it('cancels the countdown when the user comes back in time', async () => {
    const env = await started();
    env.setTabUrl(7, away);
    await onTabUpdated(env.deps, 7);
    env.advance(30_000);
    env.setTabUrl(7, REELS_URL);
    await onTabUpdated(env.deps, 7);
    expect((await readSession(env.store))?.leftSurfaceAt).toBeNull();
    expect(env.alarms.has(LEAVE_ALARM)).toBe(false);
    env.advance(120_000);
    await onAlarm(env.deps, LEAVE_ALARM); // a stale alarm must do nothing
    expect(await readSession(env.store)).not.toBeNull();
  });

  it('counts a hidden URL (not instagram.com) as left', async () => {
    const env = await started();
    env.setTabUrl(7, undefined);
    await onTabUpdated(env.deps, 7);
    expect((await readSession(env.store))?.leftSurfaceAt).not.toBeNull();
  });

  it('moving between Reels pages does not start a countdown', async () => {
    const env = await started();
    env.setTabUrl(7, 'https://www.instagram.com/reels/Cnext456/');
    await onTabUpdated(env.deps, 7);
    expect((await readSession(env.store))?.leftSurfaceAt).toBeNull();
    expect(env.alarms.has(LEAVE_ALARM)).toBe(false);
  });

  it('keeps the first leave time while the tab stays away', async () => {
    const env = await started();
    env.setTabUrl(7, away);
    await onTabUpdated(env.deps, 7);
    env.advance(20_000);
    env.setTabUrl(7, 'https://example.test/other');
    await onTabUpdated(env.deps, 7);
    expect((await readSession(env.store))?.leftSurfaceAt).toBe(START);
  });

  it('ignores other tabs', async () => {
    const env = await started();
    env.setTabUrl(99, away);
    await onTabUpdated(env.deps, 99);
    expect((await readSession(env.store))?.leftSurfaceAt).toBeNull();
  });

  it('does nothing when no session is running', async () => {
    const env = makeEnv();
    await onTabUpdated(env.deps, 7);
    expect(env.alarms.size).toBe(0);
  });
});

describe('tab close', () => {
  it('stops with tab_closed when the monitored tab closes', async () => {
    const env = await started();
    await onTabRemoved(env.deps, 7);
    expect(await readSession(env.store)).toBeNull();
    expect(env.api.stopSession).toHaveBeenCalledWith('session-1', 'tab_closed');
    expect(env.alarms.size).toBe(0);
    expect(env.badge.at(-1)).toBe(false);
  });

  it('ignores other tabs closing', async () => {
    const env = await started();
    await onTabRemoved(env.deps, 99);
    expect(await readSession(env.store)).not.toBeNull();
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });
});

describe('browser restart', () => {
  it('stops a leftover session with browser_restart', async () => {
    const env = await started();
    await onStartup(env.deps);
    expect(await readSession(env.store)).toBeNull();
    expect(env.api.stopSession).toHaveBeenCalledWith('session-1', 'browser_restart');
  });

  it('does nothing when nothing was running', async () => {
    const env = makeEnv();
    await onStartup(env.deps);
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });
});

describe('alarms for a finished session', () => {
  it('are harmless', async () => {
    const env = makeEnv();
    env.advance(120 * MINUTE);
    await onAlarm(env.deps, CAP_ALARM);
    await onAlarm(env.deps, LEAVE_ALARM);
    await onAlarm(env.deps, 'unrelated');
    expect(env.api.stopSession).not.toHaveBeenCalled();
  });
});

describe('queued events reach the server before the stop', () => {
  async function queueOne(env: Env, id = 'e1') {
    const db = await env.deps.openQueue();
    await enqueueEvent(db, { event_id: id, enqueued_at: new Date(env.now()).toISOString(), body: {} });
    db.close();
  }
  const order = (env: Env) =>
    [...env.api.postEvents.mock.invocationCallOrder.map((n) => ['events', n] as const),
     ...env.api.stopSession.mock.invocationCallOrder.map((n) => ['stop', n] as const)]
      .sort((a, b) => a[1] - b[1])
      .map(([what]) => what);

  it('uploads the queue first, then reports the stop', async () => {
    const env = await started();
    await queueOne(env);
    await stopSession(env.deps, 'user');
    expect(order(env)).toEqual(['events', 'stop']);
  });

  it('offline: the stop waits with the events, and both go out later in that order', async () => {
    const env = await started();
    await queueOne(env);
    env.api.postEvents.mockRejectedValueOnce(new ApiError(0, 'network'));
    await stopSession(env.deps, 'user');
    expect(env.api.stopSession).not.toHaveBeenCalled(); // the server session stays open for them
    expect(await readPendingStops(env.store)).toEqual([{ sessionId: 'session-1', reason: 'user' }]);
    expect(env.alarms.has(RETRY_ALARM)).toBe(true);

    env.api.postEvents.mockRejectedValueOnce(new ApiError(0, 'network'));
    await flushPendingStops(env.deps); // still offline
    expect(env.api.stopSession).not.toHaveBeenCalled();
    expect(await readPendingStops(env.store)).toHaveLength(1);

    await flushPendingStops(env.deps); // back online
    expect(order(env)).toEqual(['events', 'events', 'events', 'stop']);
    expect(await readPendingStops(env.store)).toEqual([]);
  });

  it('a browser restart while offline keeps the events and the stop for later', async () => {
    const env = await started();
    await queueOne(env);
    env.api.postEvents.mockRejectedValue(new ApiError(0, 'network'));
    await onStartup(env.deps);
    expect(await readSession(env.store)).toBeNull();
    expect(await readPendingStops(env.store)).toEqual([
      { sessionId: 'session-1', reason: 'browser_restart' },
    ]);
    env.api.postEvents.mockImplementation((events) =>
      Promise.resolve(events.map((e) => ({ event_id: e.event_id, status: 'stored' as const, reason: null }))),
    );
    await flushPendingStops(env.deps);
    expect(env.api.stopSession).toHaveBeenCalledExactlyOnceWith('session-1', 'browser_restart');
  });

  it('with consent revoked meanwhile, nothing is uploaded and the stop still goes out', async () => {
    const env = await started();
    await queueOne(env);
    await clearConsent(env.store);
    await stopSession(env.deps, 'user');
    expect(env.api.postEvents).not.toHaveBeenCalled();
    expect(env.api.stopSession).toHaveBeenCalledOnce();
  });
});

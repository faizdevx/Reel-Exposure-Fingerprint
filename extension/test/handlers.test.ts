import { describe, expect, it } from 'vitest';

import { ApiError } from '../src/api/client';
import { handleAlarm, handleRequest, handleStartup } from '../src/background/handlers';
import { enqueueEvent } from '../src/queue/db';
import { isConsentActive, isRevocationPending } from '../src/consent/store';
import { RETRY_ALARM, readSession } from '../src/session/state';
import { isRequest } from '../src/shared/messages';
import { makeEnv } from './helpers';

describe('isRequest', () => {
  it.each(['getStatus', 'grantConsent', 'revokeConsent', 'startSession', 'stopSession'])(
    'accepts %s',
    (type) => {
      expect(isRequest({ type })).toBe(true);
    },
  );

  it.each([null, undefined, 'getStatus', 42, {}, { type: 7 }, { type: 'deleteEverything' }])(
    'rejects %j',
    (value) => {
      expect(isRequest(value)).toBe(false);
    },
  );
});

describe('popup requests', () => {
  it('walks the whole flow: grant, start, stop, revoke', async () => {
    const env = makeEnv();
    env.token();

    expect(await handleRequest(env.deps, { type: 'grantConsent' })).toEqual({ ok: true });
    expect(await handleRequest(env.deps, { type: 'startSession' })).toEqual({ ok: true });
    expect(await handleRequest(env.deps, { type: 'getStatus' })).toMatchObject({
      consent: true,
      monitoring: true,
      hasToken: true,
    });
    expect(await handleRequest(env.deps, { type: 'stopSession' })).toEqual({ ok: true });
    expect(env.api.stopSession).toHaveBeenCalledWith('session-1', 'user');
    expect(await handleRequest(env.deps, { type: 'revokeConsent' })).toEqual({ ok: true });
    expect(await handleRequest(env.deps, { type: 'getStatus' })).toMatchObject({
      consent: false,
      monitoring: false,
    });
  });

  it('refuses to start before consent', async () => {
    const env = makeEnv();
    env.token();
    expect(await handleRequest(env.deps, { type: 'startSession' })).toEqual({
      ok: false,
      error: 'no_consent',
    });
  });

  it('status never exposes the token', async () => {
    const env = makeEnv();
    await env.ready();
    const status = await handleRequest(env.deps, { type: 'getStatus' });
    expect(JSON.stringify(status)).not.toContain('AbCdEf');
  });
});

describe('retry alarm', () => {
  it('retries a stop and a revocation together', async () => {
    const env = makeEnv();
    await env.ready();
    await handleRequest(env.deps, { type: 'startSession' });
    env.api.stopSession.mockRejectedValueOnce(new ApiError(0, 'network'));
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));
    await handleRequest(env.deps, { type: 'stopSession' });
    await handleRequest(env.deps, { type: 'revokeConsent' });
    expect(await isRevocationPending(env.store)).toBe(true);
    expect(env.alarms.has(RETRY_ALARM)).toBe(true);

    await handleAlarm(env.deps, RETRY_ALARM);

    expect(await isRevocationPending(env.store)).toBe(false);
    expect(env.api.deleteConsent).toHaveBeenCalledTimes(2);
    expect(env.api.stopSession).toHaveBeenCalledTimes(2);
  });

  it('is retried when the popup asks for status', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));
    await handleRequest(env.deps, { type: 'revokeConsent' });
    const status = await handleRequest(env.deps, { type: 'getStatus' });
    expect(status).toMatchObject({ revocationPending: false });
  });
});

describe('startup', () => {
  it('ends a leftover session and finishes a pending revocation', async () => {
    const env = makeEnv();
    await env.ready();
    await handleRequest(env.deps, { type: 'startSession' });
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));
    await handleRequest(env.deps, { type: 'revokeConsent' });

    await handleStartup(env.deps);

    expect(await readSession(env.store)).toBeNull();
    expect(await isConsentActive(env.store)).toBe(false);
    expect(await isRevocationPending(env.store)).toBe(false);
  });
  it('uploads events left from before the restart, then reports the leftover session stopped', async () => {
    const env = makeEnv();
    await env.ready();
    await handleRequest(env.deps, { type: 'startSession' });
    const db = await env.deps.openQueue();
    await enqueueEvent(db, { event_id: 'left-over', enqueued_at: new Date(env.now()).toISOString(), body: {} });
    db.close();

    await handleStartup(env.deps);

    expect(env.api.postEvents.mock.calls[0]?.[0].map((e) => e.event_id)).toEqual(['left-over']);
    expect(env.api.stopSession).toHaveBeenCalledExactlyOnceWith('session-1', 'browser_restart');
    expect(env.api.postEvents.mock.invocationCallOrder[0]).toBeLessThan(
      env.api.stopSession.mock.invocationCallOrder[0] ?? 0,
    );
  });
});

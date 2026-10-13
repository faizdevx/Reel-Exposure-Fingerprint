import { describe, expect, it } from 'vitest';

import { ApiError } from '../src/api/client';
import { POLICY_VERSION } from '../src/consent/policy';
import { flushPendingRevocation, grantConsent, revokeConsent } from '../src/consent/service';
import { isConsentActive, isRevocationPending, readConsent } from '../src/consent/store';
import { startSession } from '../src/session/controller';
import { RETRY_ALARM, readSession } from '../src/session/state';
import { makeEnv } from './helpers';

describe('grantConsent', () => {
  it('records the policy version with the server and stores the server grant time', async () => {
    const env = makeEnv();
    env.token();
    expect(await grantConsent(env.deps)).toEqual({ ok: true });
    expect(env.api.putConsent).toHaveBeenCalledExactlyOnceWith(POLICY_VERSION);
    expect(await isConsentActive(env.store)).toBe(true);
    expect((await readConsent(env.store))?.grantedAt).toBe(new Date(env.now()).toISOString());
  });

  it('needs a token and makes no request without one', async () => {
    const env = makeEnv();
    expect(await grantConsent(env.deps)).toEqual({ ok: false, error: 'no_token' });
    expect(env.api.putConsent).not.toHaveBeenCalled();
    expect(await isConsentActive(env.store)).toBe(false);
  });

  it.each([
    ['unreachable', new ApiError(0, 'network'), 'network'],
    ['rejecting the token', new ApiError(401, 'Not authenticated'), 'Not authenticated'],
  ])('stores nothing when the server is %s', async (_label, error, code) => {
    const env = makeEnv();
    env.token();
    env.api.putConsent.mockRejectedValueOnce(error);
    expect(await grantConsent(env.deps)).toEqual({ ok: false, error: code });
    expect(await isConsentActive(env.store)).toBe(false);
  });

  it('does not accept a response for a different policy version or a revoked consent', async () => {
    const env = makeEnv();
    env.token();
    env.api.putConsent.mockResolvedValueOnce({
      policy_version: 'other',
      granted_at: 'x',
      revoked_at: null,
    });
    expect((await grantConsent(env.deps)).ok).toBe(false);
    env.api.putConsent.mockResolvedValueOnce({
      policy_version: POLICY_VERSION,
      granted_at: 'x',
      revoked_at: 'y',
    });
    expect((await grantConsent(env.deps)).ok).toBe(false);
    expect(await isConsentActive(env.store)).toBe(false);
  });

  it('supersedes a revocation the server never heard about', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));
    await revokeConsent(env.deps);
    expect(await isRevocationPending(env.store)).toBe(true);
    await grantConsent(env.deps);
    expect(await isRevocationPending(env.store)).toBe(false);
    expect(env.alarms.has(RETRY_ALARM)).toBe(false);
  });
});

describe('revokeConsent', () => {
  it('clears consent, ends the session, purges the queue, then tells the server', async () => {
    const env = makeEnv();
    await env.ready();
    await startSession(env.deps);
    expect(await readSession(env.store)).not.toBeNull();

    expect(await revokeConsent(env.deps)).toEqual({ ok: true });

    expect(await isConsentActive(env.store)).toBe(false);
    expect(await readSession(env.store)).toBeNull();
    expect(env.purgeCount()).toBe(1);
    expect(env.api.deleteConsent).toHaveBeenCalledTimes(1);
    expect(env.badge.at(-1)).toBe(false);
    expect(env.alarms.size).toBe(0);
    // The server stops the session itself (DELETE /consent), so no separate stop call is made.
    expect(env.api.stopSession).not.toHaveBeenCalled();
    expect(await isRevocationPending(env.store)).toBe(false);
  });

  it('keeps the local revocation and retries when the server is unreachable', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));

    expect(await revokeConsent(env.deps)).toEqual({ ok: false, error: 'server_unreachable' });
    expect(await isConsentActive(env.store)).toBe(false);
    expect(await isRevocationPending(env.store)).toBe(true);
    expect(env.alarms.has(RETRY_ALARM)).toBe(true);

    await flushPendingRevocation(env.deps);
    expect(env.api.deleteConsent).toHaveBeenCalledTimes(2);
    expect(await isRevocationPending(env.store)).toBe(false);
  });

  it('retries again while the server stays down', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.deleteConsent.mockRejectedValue(new ApiError(503, 'http_503'));
    await revokeConsent(env.deps);
    env.alarms.delete(RETRY_ALARM);
    await flushPendingRevocation(env.deps);
    expect(await isRevocationPending(env.store)).toBe(true);
    expect(env.alarms.has(RETRY_ALARM)).toBe(true);
  });

  it('gives up retrying when the token is rejected: it could never succeed', async () => {
    const env = makeEnv();
    await env.ready();
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(401, 'Not authenticated'));
    expect((await revokeConsent(env.deps)).ok).toBe(false);
    expect(await isRevocationPending(env.store)).toBe(false);
    expect(await isConsentActive(env.store)).toBe(false);
  });

  it('works with no consent and no session', async () => {
    const env = makeEnv();
    env.token();
    expect(await revokeConsent(env.deps)).toEqual({ ok: true });
  });

  it('does nothing on flush when nothing is pending', async () => {
    const env = makeEnv();
    await flushPendingRevocation(env.deps);
    expect(env.api.deleteConsent).not.toHaveBeenCalled();
  });
});

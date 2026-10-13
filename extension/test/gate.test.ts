/**
 * Spec F1: with consent absent or revoked the extension emits zero events.
 * "Emits" means two things here, and both are asserted: nothing enters the upload queue, and
 * nothing is sent over the network.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import { revokeConsent } from '../src/consent/service';
import { POLICY_VERSION } from '../src/consent/policy';
import { clearConsent, writeConsent } from '../src/consent/store';
import { emitEvent, type EmitDeps } from '../src/events/emitter';
import { countEvents, openQueueDb, type QueueDb } from '../src/queue/db';
import { startSession } from '../src/session/controller';
import { writeSession } from '../src/session/state';
import { MINUTE, makeEnv, type Env } from './helpers';

let n = 0;
let env: Env;
let queue: QueueDb;
let fetchSpy: ReturnType<typeof vi.fn>;

const event = (id: string) => ({ event_id: id, body: { reel_shortcode: 'Cabc123' } });
const emitDeps = (): EmitDeps => ({ store: env.store, queue, now: env.now });
const apiCalls = () =>
  Object.values(env.api).reduce((sum, fn) => sum + fn.mock.calls.length, 0);

beforeEach(async () => {
  env = makeEnv();
  queue = await openQueueDb(`gate-${++n}`);
  fetchSpy = vi.fn();
  vi.stubGlobal('fetch', fetchSpy);
});

afterEach(() => vi.unstubAllGlobals());

/** A running session, as if Start had succeeded. Consent is set separately by each test. */
async function runningSession() {
  await writeSession(env.store, {
    sessionId: 's1',
    tabId: 7,
    startedAt: env.now(),
    leftSurfaceAt: null,
  });
}

describe('consent absent: zero events', () => {
  it('emits nothing even with a running session', async () => {
    await runningSession();
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(apiCalls()).toBe(0);
  });

  it('refuses to start a session, and makes no request', async () => {
    env.token();
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'no_consent' });
    expect(env.api.startSession).not.toHaveBeenCalled();
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['a boolean', true],
    ['a string', 'yes'],
    ['an empty object', {}],
    ['a record without a grant time', { policyVersion: POLICY_VERSION }],
    ['a record without a version', { grantedAt: '2026-10-07T00:00:00Z' }],
    ['numbers', { policyVersion: 1, grantedAt: 2 }],
    ['null', null],
  ])('treats %s as no consent', async (_label, stored) => {
    env.store.data['consent'] = stored;
    await runningSession();
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
  });

  it('treats consent given for a different policy text as no consent', async () => {
    await writeConsent(env.store, { policyVersion: 'an-older-policy', grantedAt: '2026-01-01T00:00:00Z' });
    await runningSession();
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
  });
});

describe('consent revoked: zero events', () => {
  it('stops emitting the moment consent is cleared', async () => {
    await env.ready();
    await runningSession();
    expect(await emitEvent(emitDeps(), event('before'))).toBe(true);
    expect(await countEvents(queue)).toBe(1);

    await clearConsent(env.store);
    expect(await emitEvent(emitDeps(), event('after'))).toBe(false);
    expect(await countEvents(queue)).toBe(1); // only the one from before
  });

  it('revoking ends the session, deletes unsent events and blocks new ones', async () => {
    await env.ready();
    expect(await startSession(env.deps)).toEqual({ ok: true });
    const real = await openQueueDb('gate-purge');
    const purging = { ...env.deps, purgeQueue: async () => (await real.clear('events')) };
    const gate: EmitDeps = { store: env.store, queue: real, now: env.now };
    expect(await emitEvent(gate, event('e1'))).toBe(true);
    expect(await emitEvent(gate, event('e2'))).toBe(true);
    expect(await countEvents(real)).toBe(2);

    await revokeConsent(purging);

    expect(await countEvents(real)).toBe(0);
    expect(env.alarms.size).toBe(0);
    expect(env.badge.at(-1)).toBe(false);
    expect(await emitEvent(gate, event('e3'))).toBe(false);
    expect(await countEvents(real)).toBe(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('is revoked locally even when the server cannot be reached', async () => {
    await env.ready();
    await startSession(env.deps);
    env.api.deleteConsent.mockRejectedValueOnce(new ApiError(0, 'network'));

    const result = await revokeConsent(env.deps);

    expect(result).toEqual({ ok: false, error: 'server_unreachable' });
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
    expect(env.purgeCount()).toBe(1);
  });

  it('stops emitting when the server reports consent is gone', async () => {
    await env.ready();
    env.api.startSession.mockRejectedValueOnce(new ApiError(403, 'consent_required'));
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'consent_required' });
    await runningSession(); // even if state somehow existed, the local gate is now closed
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
  });

  it('does not let a session started during a revocation survive it', async () => {
    await env.ready();
    env.api.startSession.mockImplementationOnce(async () => {
      await clearConsent(env.store); // the user revokes while the request is in flight
      return { id: 's9', started_at: new Date(env.now()).toISOString(), ended_at: null, status: 'active', stop_reason: null };
    });
    expect(await startSession(env.deps)).toEqual({ ok: false, error: 'no_consent' });
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(env.badge).not.toContain(true);
    expect(env.api.stopSession).toHaveBeenCalledWith('s9', 'user'); // the orphan is closed
  });
});

describe('consent given but no live session: zero events', () => {
  it('emits nothing before Start', async () => {
    await env.ready();
    expect(await emitEvent(emitDeps(), event('e1'))).toBe(false);
    expect(await countEvents(queue)).toBe(0);
  });

  it('emits nothing once the 60 minute cap has passed, even before the alarm fires', async () => {
    await env.ready();
    await startSession(env.deps);
    expect(await emitEvent(emitDeps(), event('inside'))).toBe(true);
    env.advance(60 * MINUTE);
    expect(await emitEvent(emitDeps(), event('over'))).toBe(false);
    expect(await countEvents(queue)).toBe(1);
  });

  it('emits nothing 60 seconds after leaving the Reels surface', async () => {
    await env.ready();
    await writeSession(env.store, {
      sessionId: 's1',
      tabId: 7,
      startedAt: env.now(),
      leftSurfaceAt: env.now(),
    });
    env.advance(59_000);
    expect(await emitEvent(emitDeps(), event('still-ok'))).toBe(true);
    env.advance(1_000);
    expect(await emitEvent(emitDeps(), event('too-late'))).toBe(false);
  });
});

describe('the gate cannot be bypassed', () => {
  const SRC = join(__dirname, '..', 'src');

  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? sourceFiles(path) : /\.(ts|js)$/.test(name) ? [path] : [];
    });
  }

  const read = (path: string) => readFileSync(path, 'utf8');
  const rel = (path: string) => relative(SRC, path).replaceAll('\\', '/');

  it('only the emitter writes events to the upload queue', () => {
    const writers = sourceFiles(SRC)
      .filter((f) => /\benqueueEvent\s*\(/.test(read(f)))
      .map(rel);
    // db.ts defines it; emitter.ts is the single caller.
    expect(writers.sort()).toEqual(['events/emitter.ts', 'queue/db.ts']);
  });

  it('only the API client touches the network', () => {
    const callers = sourceFiles(SRC)
      .filter((f) => /\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket/.test(read(f)))
      .map(rel);
    expect(callers).toEqual(['api/client.ts']);
  });
});

import { beforeEach, describe, expect, it } from 'vitest';

import { revokeConsent } from '../src/consent/service';
import { isContentActive } from '../src/session/content-state';
import { onAlarm, onTabRemoved, onTabUpdated, startSession, stopSession } from '../src/session/controller';
import { CAP_ALARM } from '../src/session/state';
import { MINUTE, REELS_URL, makeEnv, type Env } from './helpers';

let env: Env;
beforeEach(() => {
  env = makeEnv();
});

describe('session state pushes to the content script', () => {
  it('tells the monitored tab on start and on stop', async () => {
    await env.ready();
    await startSession(env.deps);
    await stopSession(env.deps, 'user');
    expect(env.notified).toEqual([
      { tabId: 7, active: true },
      { tabId: 7, active: false },
    ]);
  });

  it('tells the tab off when consent is revoked', async () => {
    await env.ready();
    await startSession(env.deps);
    await revokeConsent(env.deps);
    expect(env.notified.at(-1)).toEqual({ tabId: 7, active: false });
  });

  it('tells the tab off on cap, and when the tab is closed', async () => {
    await env.ready();
    await startSession(env.deps);
    env.advance(60 * MINUTE);
    await onAlarm(env.deps, CAP_ALARM);
    expect(env.notified.at(-1)).toEqual({ tabId: 7, active: false });

    await startSession(env.deps);
    await onTabRemoved(env.deps, 7);
    expect(env.notified.at(-1)).toEqual({ tabId: 7, active: false });
  });

  it('follows the tab off and back onto the Reels surface', async () => {
    await env.ready();
    await startSession(env.deps);
    env.setTabUrl(7, 'https://www.instagram.com/');
    await onTabUpdated(env.deps, 7);
    expect(env.notified.at(-1)).toEqual({ tabId: 7, active: false });
    env.setTabUrl(7, REELS_URL);
    await onTabUpdated(env.deps, 7);
    expect(env.notified.at(-1)).toEqual({ tabId: 7, active: true });
  });

  it('does not push for a tab that is not monitored', async () => {
    await env.ready();
    await startSession(env.deps);
    const before = env.notified.length;
    await onTabUpdated(env.deps, 99);
    expect(env.notified).toHaveLength(before);
  });

  it('pushes nothing when a start is refused', async () => {
    env.token(); // no consent
    await startSession(env.deps);
    expect(env.notified).toEqual([]);
  });
});

describe('isContentActive', () => {
  it('needs consent, a session, the same tab, the Reels URL and no automatic stop due', async () => {
    await env.ready();
    await startSession(env.deps);
    const active = (tab: number | undefined, url: string | undefined) =>
      isContentActive(env.store, env.now(), tab, url);
    expect(await active(7, REELS_URL)).toBe(true);
    expect(await active(8, REELS_URL)).toBe(false);
    expect(await active(undefined, REELS_URL)).toBe(false);
    expect(await active(7, undefined)).toBe(false);
    env.advance(60 * MINUTE);
    expect(await active(7, REELS_URL)).toBe(false); // cap reached, alarm not fired yet
  });
});

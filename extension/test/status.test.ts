import { describe, expect, it } from 'vitest';

import { TOKEN_KEY } from '../src/auth/token';
import { readStatus } from '../src/background/status';
import { POLICY_VERSION } from '../src/consent/policy';
import { writeConsent } from '../src/consent/store';
import { writeSession } from '../src/session/state';
import { TOKEN, memoryStore } from './helpers';

const session = { sessionId: 's1', tabId: 1, startedAt: 0, leftSurfaceAt: null };
const consent = { policyVersion: POLICY_VERSION, grantedAt: '2026-10-07T12:00:00Z' };

describe('readStatus', () => {
  it('is off when nothing is stored', async () => {
    expect(await readStatus(memoryStore())).toEqual({
      consent: false,
      consentGrantedAt: null,
      revocationPending: false,
      monitoring: false,
      hasToken: false,
    });
  });

  it('never reports monitoring without consent, even with a stored session', async () => {
    const store = memoryStore();
    await writeSession(store, session);
    expect(await readStatus(store)).toMatchObject({ consent: false, monitoring: false });
  });

  it('reports monitoring only with consent and a session', async () => {
    const store = memoryStore();
    await writeConsent(store, consent);
    expect(await readStatus(store)).toMatchObject({ consent: true, monitoring: false });
    await writeSession(store, session);
    expect(await readStatus(store)).toMatchObject({
      consent: true,
      consentGrantedAt: consent.grantedAt,
      monitoring: true,
    });
  });

  it.each([true, 'yes', 1, {}])('treats the old boolean-style value %j as no consent', async (value) => {
    const store = memoryStore();
    store.data['consent'] = value;
    expect(await readStatus(store)).toMatchObject({ consent: false });
  });

  it('reports a stored token without ever returning it', async () => {
    const store = memoryStore();
    store.data[TOKEN_KEY] = TOKEN;
    const status = await readStatus(store);
    expect(status.hasToken).toBe(true);
    expect(JSON.stringify(status)).not.toContain(TOKEN);
  });
});

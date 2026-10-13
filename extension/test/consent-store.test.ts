import { describe, expect, it } from 'vitest';

import { POLICY_VERSION } from '../src/consent/policy';
import {
  clearConsent,
  isConsentActive,
  isRevocationPending,
  readConsent,
  setRevocationPending,
  writeConsent,
} from '../src/consent/store';
import { memoryStore } from './helpers';

describe('local consent', () => {
  it('is inactive until recorded, active after, inactive after clearing', async () => {
    const store = memoryStore();
    expect(await isConsentActive(store)).toBe(false);
    await writeConsent(store, { policyVersion: POLICY_VERSION, grantedAt: '2026-10-07T12:00:00Z' });
    expect(await isConsentActive(store)).toBe(true);
    expect(await readConsent(store)).toEqual({
      policyVersion: POLICY_VERSION,
      grantedAt: '2026-10-07T12:00:00Z',
    });
    await clearConsent(store);
    expect(await isConsentActive(store)).toBe(false);
    expect(await readConsent(store)).toBeNull();
  });

  it('does not count consent given for another policy version', async () => {
    const store = memoryStore();
    await writeConsent(store, { policyVersion: 'old', grantedAt: '2026-10-07T12:00:00Z' });
    expect(await isConsentActive(store)).toBe(false);
  });

  it('tracks a revocation the server has not confirmed', async () => {
    const store = memoryStore();
    expect(await isRevocationPending(store)).toBe(false);
    await setRevocationPending(store, true);
    expect(await isRevocationPending(store)).toBe(true);
    await setRevocationPending(store, false);
    expect(await isRevocationPending(store)).toBe(false);
  });
});

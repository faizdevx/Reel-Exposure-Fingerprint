import { createHash } from 'node:crypto';

import { describe, expect, it } from 'vitest';

import { POLICY, POLICY_VERSION } from '../src/consent/policy';

// Consent integrity: a person agreed to specific words. If this hash changes, the wording changed,
// so POLICY_VERSION must change too (existing consent then stops counting). Update both together.
const POLICY_HASH = 'c9d2d4a327d2a576aa44dff53e94ffe68375ff88e78174fc5c191e6c04d75704';

describe('consent policy', () => {
  it('has a plain version string the server accepts', () => {
    expect(POLICY_VERSION).toMatch(/^[A-Za-z0-9._-]{1,32}$/);
  });

  it('changes version whenever its text changes', () => {
    const hash = createHash('sha256').update(JSON.stringify(POLICY)).digest('hex');
    expect({ version: POLICY_VERSION, hash }).toEqual({
      version: '2026-10-07.1',
      hash: POLICY_HASH,
    });
  });

  it.each(['intro', 'collected', 'derived', 'never', 'when', 'control'] as const)(
    'has a non-empty %s section',
    (key) => {
      expect(POLICY[key].length).toBeGreaterThan(0);
    },
  );

  const all = JSON.stringify(POLICY).toLowerCase();

  it('lists what is collected', () => {
    for (const phrase of [
      'reel id',
      'handle',
      'caption',
      'hashtags',
      'how long',
      '5 small still images',
      '24 hours',
    ]) {
      expect(all).toContain(phrase);
    }
  });

  it('says what is never collected', () => {
    for (const phrase of [
      'password',
      'cookies',
      'other websites',
      'likes, comments',
      'monitoring is off',
    ]) {
      expect(all).toContain(phrase);
    }
  });

  it('states the session limits from the spec', () => {
    expect(all).toContain('60 seconds');
    expect(all).toContain('60 minutes');
    expect(all).toContain('close the tab');
  });

  it('explains revocation, and does not promise deletion that does not exist yet', () => {
    expect(all).toContain('revoke consent at any time');
    expect(all).toContain('not available yet');
  });
});

import { describe, expect, it } from 'vitest';

import { describeError, viewFor } from '../src/popup/render';
import type { StatusResponse } from '../src/shared/messages';

const status = (over: Partial<StatusResponse> = {}): StatusResponse => ({
  consent: false,
  consentGrantedAt: null,
  revocationPending: false,
  monitoring: false,
  hasToken: false,
  ...over,
});

describe('viewFor', () => {
  it('shows the consent screen and disables Start when there is no consent', () => {
    const view = viewFor(status({ hasToken: true }));
    expect(view.consentScreen).toBe(true);
    expect(view.sessionButtonDisabled).toBe(true);
    expect(view.statusText).toContain('No consent');
  });

  it('shows an error state when the background is unreachable', () => {
    const view = viewFor(null);
    expect(view.sessionButtonDisabled).toBe(true);
    expect(view.consentScreen).toBe(false);
    expect(view.statusText).toContain('Cannot reach');
  });

  it('hides the consent text and offers Start once consent and a token exist', () => {
    const view = viewFor(
      status({ consent: true, hasToken: true, consentGrantedAt: '2026-10-07T12:00:00Z' }),
    );
    expect(view.consentScreen).toBe(false);
    expect(view.consentSummary).toContain('Consent given');
    expect(view.sessionButtonLabel).toBe('Start monitoring');
    expect(view.sessionButtonDisabled).toBe(false);
  });

  it('needs a token to start', () => {
    const view = viewFor(status({ consent: true, hasToken: false }));
    expect(view.sessionButtonDisabled).toBe(true);
    expect(view.note).toContain('token');
  });

  it('offers Stop while monitoring', () => {
    const view = viewFor(status({ consent: true, hasToken: true, monitoring: true }));
    expect(view.statusText).toBe('Monitoring is on.');
    expect(view.sessionButtonLabel).toBe('Stop monitoring');
    expect(view.sessionButtonDisabled).toBe(false);
  });

  it('says when a revocation is waiting for the server', () => {
    expect(viewFor(status({ revocationPending: true })).note).toContain('retrying');
  });

  it('says whether a token is saved', () => {
    expect(viewFor(status({ hasToken: true })).tokenText).toContain('saved');
    expect(viewFor(status()).tokenText).toContain('No extension token');
  });
});

describe('describeError', () => {
  it('explains the known errors and falls back for unknown ones', () => {
    expect(describeError('not_on_reels')).toContain('instagram.com/reels');
    expect(describeError('no_consent')).toContain('consent');
    expect(describeError('something_else')).toBe('Failed (something_else).');
  });
});

describe('monitoring badge', () => {
  it('is visible only while monitoring', () => {
    expect(viewFor(status({ consent: true, hasToken: true, monitoring: true })).badge).toBe(true);
    expect(viewFor(status({ consent: true, hasToken: true })).badge).toBe(false);
    expect(viewFor(status()).badge).toBe(false);
    expect(viewFor(null).badge).toBe(false);
  });
});

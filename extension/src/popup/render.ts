import type { StatusResponse } from '../shared/messages';

export interface PopupView {
  /** The red monitoring badge is visible. True only while monitoring is on. */
  badge: boolean;
  statusText: string;
  tokenText: string;
  /** Show the full consent text and the grant controls. */
  consentScreen: boolean;
  /** Shown instead of the consent text once consent is recorded. */
  consentSummary: string;
  sessionButtonLabel: string;
  sessionButtonDisabled: boolean;
  note: string;
}

/** Pure view model so the popup logic is testable without a DOM. */
export function viewFor(status: StatusResponse | null): PopupView {
  if (status === null) {
    return {
      badge: false,
      statusText: 'Cannot reach the extension background.',
      tokenText: '',
      consentScreen: false,
      consentSummary: '',
      sessionButtonLabel: 'Start monitoring',
      sessionButtonDisabled: true,
      note: '',
    };
  }

  const tokenText = status.hasToken
    ? 'Extension token saved.'
    : 'No extension token. Create one in the dashboard and paste it below.';
  const pending = status.revocationPending
    ? 'Consent is revoked here. The server has not confirmed yet; retrying.'
    : '';

  if (!status.consent) {
    return {
      badge: false,
      statusText: 'Monitoring is off. No consent recorded.',
      tokenText,
      consentScreen: true,
      consentSummary: '',
      sessionButtonLabel: 'Start monitoring',
      sessionButtonDisabled: true,
      note: pending,
    };
  }

  const canStart = status.hasToken || status.monitoring;
  return {
    badge: status.monitoring,
    statusText: status.monitoring ? 'Monitoring is on.' : 'Monitoring is off.',
    tokenText,
    consentScreen: false,
    consentSummary: status.consentGrantedAt
      ? `Consent given ${new Date(status.consentGrantedAt).toLocaleString()}.`
      : 'Consent given.',
    sessionButtonLabel: status.monitoring ? 'Stop monitoring' : 'Start monitoring',
    sessionButtonDisabled: !canStart,
    note: pending || (canStart ? '' : 'Save an extension token to start.'),
  };
}

const ERROR_TEXT: Record<string, string> = {
  no_token: 'Save your extension token first.',
  no_consent: 'Give consent first.',
  consent_required: 'The server has no consent for you. Give consent again.',
  not_on_reels: 'Open instagram.com/reels in this tab, then press Start.',
  already_active: 'A session is already running.',
  not_active: 'No session is running.',
  network: 'Cannot reach the server.',
  server_unreachable: 'Consent revoked here. Cannot reach the server to confirm; will retry.',
  unexpected_response: 'The server answered unexpectedly.',
  http_401: 'The server rejected the extension token. Save a new one.',
};

export function describeError(code: string): string {
  return ERROR_TEXT[code] ?? `Failed (${code}).`;
}

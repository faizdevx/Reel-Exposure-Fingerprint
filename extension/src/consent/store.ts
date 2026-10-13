import type { Store } from '../runtime';
import { POLICY_VERSION } from './policy';

const CONSENT_KEY = 'consent';
const PENDING_REVOCATION_KEY = 'pendingRevocation';

export interface LocalConsent {
  policyVersion: string;
  /** The server's grant time, as returned by PUT /consent. */
  grantedAt: string;
}

export async function readConsent(store: Store): Promise<LocalConsent | null> {
  const value = (await store.get([CONSENT_KEY]))[CONSENT_KEY];
  if (typeof value !== 'object' || value === null) return null;
  const { policyVersion, grantedAt } = value as Record<string, unknown>;
  if (typeof policyVersion !== 'string' || typeof grantedAt !== 'string') return null;
  return { policyVersion, grantedAt };
}

/**
 * The extension-side half of the consent gate (spec F1). Anything missing, malformed, or given for
 * a different policy text means NO consent: collection must not start or continue.
 */
export async function isConsentActive(store: Store): Promise<boolean> {
  const consent = await readConsent(store);
  return consent !== null && consent.policyVersion === POLICY_VERSION;
}

export async function writeConsent(store: Store, consent: LocalConsent): Promise<void> {
  await store.set({ [CONSENT_KEY]: consent });
}

export async function clearConsent(store: Store): Promise<void> {
  await store.remove([CONSENT_KEY]);
}

/** Set when the local revocation succeeded but telling the server failed. */
export async function isRevocationPending(store: Store): Promise<boolean> {
  return (await store.get([PENDING_REVOCATION_KEY]))[PENDING_REVOCATION_KEY] === true;
}

export async function setRevocationPending(store: Store, pending: boolean): Promise<void> {
  if (pending) await store.set({ [PENDING_REVOCATION_KEY]: true });
  else await store.remove([PENDING_REVOCATION_KEY]);
}

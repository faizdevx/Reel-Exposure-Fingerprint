import { hasToken } from '../auth/token';
import { isConsentActive, isRevocationPending, readConsent } from '../consent/store';
import type { Store } from '../runtime';
import { readSession } from '../session/state';
import type { StatusResponse } from '../shared/messages';

/** Missing or malformed values mean "off": nothing is collected without explicit consent. */
export async function readStatus(store: Store): Promise<StatusResponse> {
  const consent = await isConsentActive(store);
  return {
    consent,
    consentGrantedAt: consent ? ((await readConsent(store))?.grantedAt ?? null) : null,
    revocationPending: await isRevocationPending(store),
    monitoring: consent && (await readSession(store)) !== null,
    hasToken: await hasToken(store),
  };
}

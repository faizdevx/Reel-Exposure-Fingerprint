import { ApiError } from '../api/client';
import { hasToken } from '../auth/token';
import type { Deps } from '../runtime';
import { clearUploadState } from '../upload/scheduler';
import { RETRY_ALARM } from '../session/state';
import { endLocalSession, type ActionResult } from '../session/controller';
import { POLICY_VERSION } from './policy';
import {
  clearConsent,
  isRevocationPending,
  setRevocationPending,
  writeConsent,
} from './store';

const RETRY_MS = 60 * 1000;

/**
 * Record consent on the server, then locally. Local consent exists only after the server has
 * confirmed it, so the two never disagree in the "granted" direction.
 */
export async function grantConsent(deps: Deps): Promise<ActionResult> {
  if (!(await hasToken(deps.store))) return { ok: false, error: 'no_token' };
  try {
    const dto = await deps.api.putConsent(POLICY_VERSION);
    if (dto.policy_version !== POLICY_VERSION || dto.revoked_at !== null) {
      return { ok: false, error: 'unexpected_response' };
    }
    await writeConsent(deps.store, { policyVersion: dto.policy_version, grantedAt: dto.granted_at });
  } catch (error) {
    return { ok: false, error: error instanceof ApiError ? error.code : 'unknown' };
  }
  // A fresh grant supersedes a revocation that never reached the server.
  await setRevocationPending(deps.store, false);
  await deps.alarms.clear(RETRY_ALARM);
  return { ok: true };
}

/**
 * Revoke consent. Everything local happens first and cannot fail on the network: consent is
 * cleared, any session ends, the unsent queue is deleted. Only then is the server told; if that
 * fails the revocation is remembered and retried. (Spec F1, CONTRIBUTING.md privacy rules.)
 */
export async function revokeConsent(deps: Deps): Promise<ActionResult> {
  await clearConsent(deps.store);
  await endLocalSession(deps);
  await deps.purgeQueue();
  await clearUploadState(deps);
  return tellServerAboutRevocation(deps);
}

/** Retry a revocation the server has not heard about yet. */
export async function flushPendingRevocation(deps: Deps): Promise<void> {
  if (await isRevocationPending(deps.store)) await tellServerAboutRevocation(deps);
}

async function tellServerAboutRevocation(deps: Deps): Promise<ActionResult> {
  try {
    await deps.api.deleteConsent();
  } catch (error) {
    if (error instanceof ApiError && (error.code === 'network' || error.status >= 500)) {
      await setRevocationPending(deps.store, true);
      await deps.alarms.create(RETRY_ALARM, deps.now() + RETRY_MS);
      return { ok: false, error: 'server_unreachable' };
    }
    // 401 (token gone) or similar: it cannot succeed later. Local revocation stands.
    await setRevocationPending(deps.store, false);
    return { ok: false, error: error instanceof ApiError ? error.code : 'unknown' };
  }
  await setRevocationPending(deps.store, false);
  return { ok: true };
}

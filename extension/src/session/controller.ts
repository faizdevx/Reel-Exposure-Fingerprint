/**
 * Monitoring sessions on the extension side (spec F2): manual start and stop, the 60 minute cap,
 * auto-stop after 60 seconds off the Reels surface, tab close and browser restart.
 *
 * Rule of thumb throughout: stop collecting locally FIRST, then tell the server. A failed or slow
 * request never keeps a session alive.
 */

import { ApiError, type StopReason } from '../api/client';
import { hasToken } from '../auth/token';
import { clearConsent, isConsentActive } from '../consent/store';
import { LEAVE_SURFACE_MS, MAX_SESSION_MS } from '../config';
import type { Deps } from '../runtime';
import {
  CAP_ALARM,
  LEAVE_ALARM,
  RETRY_ALARM,
  clearSession,
  readPendingStops,
  readSession,
  stopReasonDue,
  writePendingStops,
  writeSession,
  type ActiveSession,
  type PendingStop,
} from './state';
import { flushQueue } from '../upload/scheduler';
import { isReelsSurface } from './surface';

export type ActionResult = { ok: true } | { ok: false; error: string };

const fail = (error: string): ActionResult => ({ ok: false, error });

/** Delay before retrying a stop or revocation the server could not be told about. */
const RETRY_MS = 60 * 1000;

/** End collection on this device: state, alarms and badge. Returns what was running, if anything. */
export async function endLocalSession(deps: Deps): Promise<ActiveSession | null> {
  const session = await readSession(deps.store);
  await clearSession(deps.store);
  await deps.alarms.clear(CAP_ALARM);
  await deps.alarms.clear(LEAVE_ALARM);
  await deps.setBadge(false);
  if (session !== null) await deps.notifyContent(session.tabId, false);
  return session;
}

export async function startSession(deps: Deps): Promise<ActionResult> {
  await flushPendingStops(deps);

  if (!(await isConsentActive(deps.store))) return fail('no_consent');
  if (!(await hasToken(deps.store))) return fail('no_token');
  if ((await readSession(deps.store)) !== null) return fail('already_active');

  const tab = await deps.getActiveTab();
  if (tab === undefined || tab.id === undefined || !isReelsSurface(tab.url)) {
    return fail('not_on_reels');
  }

  let dto;
  try {
    dto = await deps.api.startSession();
  } catch (error) {
    if (error instanceof ApiError && error.code === 'consent_required') {
      // The server says there is no consent (revoked from the dashboard, say): believe it.
      await clearConsent(deps.store);
      return fail('consent_required');
    }
    return fail(error instanceof ApiError ? error.code : 'unknown');
  }

  // Consent may have been revoked while the request was in flight.
  if (!(await isConsentActive(deps.store))) {
    await reportStop(deps, dto.id, 'user');
    return fail('no_consent');
  }

  const parsed = Date.parse(dto.started_at);
  const startedAt = Number.isNaN(parsed) ? deps.now() : parsed;
  await writeSession(deps.store, {
    sessionId: dto.id,
    tabId: tab.id,
    startedAt,
    leftSurfaceAt: null,
  });
  await deps.alarms.create(CAP_ALARM, startedAt + MAX_SESSION_MS);
  await deps.setBadge(true);
  await deps.notifyContent(tab.id, true);
  return { ok: true };
}

export async function stopSession(deps: Deps, reason: StopReason): Promise<ActionResult> {
  const session = await endLocalSession(deps);
  if (session === null) return fail('not_active');
  await reportStop(deps, session.sessionId, reason);
  return { ok: true };
}

/**
 * Tell the server the session stopped. The server refuses events of a stopped session, so queued
 * events go first: if they cannot be uploaded now (offline), the stop waits with them and both are
 * retried. A transient failure of the stop itself is remembered and retried; a permanent one is
 * dropped.
 */
async function reportStop(deps: Deps, sessionId: string, reason: StopReason): Promise<void> {
  if ((await flushQueue(deps)) === 'failed') {
    await rememberStop(deps, { sessionId, reason });
    return;
  }
  try {
    await deps.api.stopSession(sessionId, reason);
  } catch (error) {
    if (isTransient(error)) await rememberStop(deps, { sessionId, reason });
  }
}

async function rememberStop(deps: Deps, stop: PendingStop): Promise<void> {
  const pending = await readPendingStops(deps.store);
  await writePendingStops(deps.store, [...pending, stop]);
  await deps.alarms.create(RETRY_ALARM, deps.now() + RETRY_MS);
}

function isTransient(error: unknown): boolean {
  return error instanceof ApiError && (error.code === 'network' || error.status >= 500);
}

export async function flushPendingStops(deps: Deps): Promise<void> {
  const pending = await readPendingStops(deps.store);
  if (pending.length === 0) return;
  // Events before stops (see reportStop). Still offline: keep everything for the next retry.
  if ((await flushQueue(deps)) === 'failed') {
    await deps.alarms.create(RETRY_ALARM, deps.now() + RETRY_MS);
    return;
  }
  const stillPending = [];
  for (const stop of pending) {
    try {
      await deps.api.stopSession(stop.sessionId, stop.reason);
    } catch (error) {
      if (isTransient(error)) stillPending.push(stop);
    }
  }
  await writePendingStops(deps.store, stillPending);
  if (stillPending.length > 0) await deps.alarms.create(RETRY_ALARM, deps.now() + RETRY_MS);
}

/** The monitored tab was closed. */
export async function onTabRemoved(deps: Deps, tabId: number): Promise<void> {
  const session = await readSession(deps.store);
  if (session !== null && session.tabId === tabId) await stopSession(deps, 'tab_closed');
}

/**
 * The monitored tab changed. Leaving the Reels surface starts a 60 second countdown; coming back
 * before it ends cancels it. A tab URL the browser hides is not instagram.com, so it counts as left.
 */
export async function onTabUpdated(deps: Deps, tabId: number): Promise<void> {
  const session = await readSession(deps.store);
  if (session === null || session.tabId !== tabId) return;

  const onSurface = isReelsSurface(await deps.getTabUrl(tabId));
  if (!onSurface && session.leftSurfaceAt === null) {
    const leftSurfaceAt = deps.now();
    await writeSession(deps.store, { ...session, leftSurfaceAt });
    await deps.alarms.create(LEAVE_ALARM, leftSurfaceAt + LEAVE_SURFACE_MS);
  } else if (onSurface && session.leftSurfaceAt !== null) {
    await writeSession(deps.store, { ...session, leftSurfaceAt: null });
    await deps.alarms.clear(LEAVE_ALARM);
  }
  // Content scripts follow SPA navigation through this push; they watch nothing themselves.
  await deps.notifyContent(tabId, onSurface);
}

/** The session-cap and off-surface alarms. (The retry alarm is routed in background/handlers.ts.) */
export async function onAlarm(deps: Deps, name: string): Promise<void> {
  if (name !== CAP_ALARM && name !== LEAVE_ALARM) return;
  const session = await readSession(deps.store);
  if (session === null) return;
  const reason = stopReasonDue(session, deps.now());
  if (reason !== null) await stopSession(deps, reason);
}

/** The browser started: a session left over from before cannot still be running. */
export async function onStartup(deps: Deps): Promise<void> {
  if ((await readSession(deps.store)) !== null) await stopSession(deps, 'browser_restart');
}

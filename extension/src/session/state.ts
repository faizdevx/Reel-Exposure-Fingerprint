import { LEAVE_SURFACE_MS, MAX_SESSION_MS } from '../config';
import type { StopReason } from '../api/client';
import type { Store } from '../runtime';

const SESSION_KEY = 'activeSession';
const PENDING_STOPS_KEY = 'pendingStops';

export interface ActiveSession {
  sessionId: string;
  /** The tab the user pressed Start in. Closing it, or leaving Reels in it, ends the session. */
  tabId: number;
  /** Milliseconds since the epoch, from the server's started_at. */
  startedAt: number;
  /** When the tab left the Reels surface, or null while it is on it. */
  leftSurfaceAt: number | null;
}

export interface PendingStop {
  sessionId: string;
  reason: StopReason;
}

export const CAP_ALARM = 'ref-session-cap';
export const LEAVE_ALARM = 'ref-left-surface';
export const RETRY_ALARM = 'ref-retry';

/**
 * Which automatic rule, if any, says this session must end now (spec F2).
 * Both thresholds are inclusive: an alarm that fires exactly on time must be able to stop it.
 */
export function stopReasonDue(
  session: ActiveSession,
  now: number,
): Extract<StopReason, 'time_cap' | 'left_surface'> | null {
  if (now - session.startedAt >= MAX_SESSION_MS) return 'time_cap';
  if (session.leftSurfaceAt !== null && now - session.leftSurfaceAt >= LEAVE_SURFACE_MS) {
    return 'left_surface';
  }
  return null;
}

export async function readSession(store: Store): Promise<ActiveSession | null> {
  const value = (await store.get([SESSION_KEY]))[SESSION_KEY];
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  const left = v['leftSurfaceAt'];
  if (
    typeof v['sessionId'] !== 'string' ||
    typeof v['tabId'] !== 'number' ||
    typeof v['startedAt'] !== 'number' ||
    !(left === null || typeof left === 'number')
  ) {
    return null;
  }
  return { sessionId: v['sessionId'], tabId: v['tabId'], startedAt: v['startedAt'], leftSurfaceAt: left };
}

export async function writeSession(store: Store, session: ActiveSession): Promise<void> {
  await store.set({ [SESSION_KEY]: session });
}

export async function clearSession(store: Store): Promise<void> {
  await store.remove([SESSION_KEY]);
}

export async function readPendingStops(store: Store): Promise<PendingStop[]> {
  const value = (await store.get([PENDING_STOPS_KEY]))[PENDING_STOPS_KEY];
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is PendingStop =>
      typeof item === 'object' &&
      item !== null &&
      typeof (item as PendingStop).sessionId === 'string' &&
      typeof (item as PendingStop).reason === 'string',
  );
}

export async function writePendingStops(store: Store, stops: readonly PendingStop[]): Promise<void> {
  if (stops.length === 0) await store.remove([PENDING_STOPS_KEY]);
  else await store.set({ [PENDING_STOPS_KEY]: [...stops] });
}

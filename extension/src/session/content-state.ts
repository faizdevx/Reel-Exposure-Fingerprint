import { isConsentActive } from '../consent/store';
import type { Store } from '../runtime';
import { readSession, stopReasonDue } from './state';
import { isReelsSurface } from './surface';

/**
 * Should this tab show the monitoring marker and be allowed to report Reels? Only the tab the user
 * started monitoring in, on the Reels surface, with consent, before any automatic stop is due.
 */
export async function isContentActive(
  store: Store,
  now: number,
  tabId: number | undefined,
  url: string | undefined,
): Promise<boolean> {
  if (tabId === undefined || !isReelsSurface(url)) return false;
  if (!(await isConsentActive(store))) return false;
  const session = await readSession(store);
  return session !== null && session.tabId === tabId && stopReasonDue(session, now) === null;
}

/**
 * May this tab report a finished viewing? The URL is not checked: a viewing ends when the user
 * navigates away, so by the time the event arrives the tab is often no longer on Reels. The
 * content script only starts a viewing on the surface (and the adapter tracker only runs there).
 */
export async function isSessionTab(
  store: Store,
  now: number,
  tabId: number | undefined,
): Promise<boolean> {
  if (tabId === undefined || !(await isConsentActive(store))) return false;
  const session = await readSession(store);
  return session !== null && session.tabId === tabId && stopReasonDue(session, now) === null;
}

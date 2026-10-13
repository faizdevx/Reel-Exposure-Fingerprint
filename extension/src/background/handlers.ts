import { flushPendingRevocation, grantConsent, revokeConsent } from '../consent/service';
import type { Deps } from '../runtime';
import {
  flushPendingStops,
  onAlarm,
  onStartup,
  startSession,
  stopSession,
} from '../session/controller';
import { RETRY_ALARM } from '../session/state';
import { emitEvent } from '../events/emitter';
import { isContentActive, isSessionTab } from '../session/content-state';
import { readSession } from '../session/state';
import {
  isContentRequest,
  isRequest,
  type ActionResponse,
  type ContentRequest,
  type ContentState,
  type EventResponse,
  type Request,
  type StatusResponse,
} from '../shared/messages';
import {
  UPLOAD_ALARM,
  flushQueue,
  onEventQueued,
  onUploadDue,
  resumeUpload,
} from '../upload/scheduler';
import { readStatus } from './status';

/** Who sent a runtime message, reduced to what routing needs. */
export interface Sender {
  /** Extension id of the sending context. */
  id: string | undefined;
  /** Set only for content scripts (and extension pages opened as tabs). */
  tabId: number | undefined;
  tabUrl: string | undefined;
}

export type Reply = StatusResponse | ActionResponse | ContentState | EventResponse;

/**
 * The one entry point for runtime messages. Two separate vocabularies, split by sender:
 * - the popup (no tab) may drive consent, token-backed sessions and status;
 * - a content script (has a tab, runs on instagram.com) may only say hello and report a Reel.
 * A message of the wrong kind for its sender, or from another extension, gets no answer at all.
 */
export function routeMessage(
  deps: Deps,
  ownId: string,
  message: unknown,
  sender: Sender,
): Promise<Reply> | null {
  if (sender.id !== ownId) return null;
  if (sender.tabId !== undefined) {
    return isContentRequest(message) ? handleContentRequest(deps, message, sender) : null;
  }
  return isRequest(message) ? handleRequest(deps, message) : null;
}

/** Content script requests. The tab and URL come from the browser, never from the message. */
export async function handleContentRequest(
  deps: Deps,
  request: ContentRequest,
  sender: Sender,
): Promise<ContentState | EventResponse> {
  if (request.type === 'contentHello') {
    return { active: await isContentActive(deps.store, deps.now(), sender.tabId, sender.tabUrl) };
  }
  if (!(await isSessionTab(deps.store, deps.now(), sender.tabId))) return { accepted: false };

  const session = await readSession(deps.store);
  if (session === null) return { accepted: false };
  const queue = await deps.openQueue();
  let accepted: boolean;
  try {
    // emitEvent re-checks consent and the session; session_id is set here, never by the page side.
    accepted = await emitEvent(
      { store: deps.store, queue, now: deps.now },
      { event_id: request.event_id, body: { ...request.body, session_id: session.sessionId } },
    );
  } finally {
    queue.close();
  }
  if (accepted) {
    // The page is going away (pagehide): send now rather than in 15 s, while the browser runs.
    if (request.flush === true) await flushQueue(deps);
    else await onEventQueued(deps);
  }
  return { accepted };
}

/** Popup requests. The service worker owns consent, sessions and the token. */
export async function handleRequest(
  deps: Deps,
  request: Request,
): Promise<StatusResponse | ActionResponse> {
  switch (request.type) {
    case 'getStatus':
      await flushPendingRevocation(deps);
      return readStatus(deps.store);
    case 'grantConsent':
      return grantConsent(deps);
    case 'revokeConsent':
      return revokeConsent(deps);
    case 'startSession':
      return startSession(deps);
    case 'stopSession':
      return stopSession(deps, 'user');
  }
}

export async function handleAlarm(deps: Deps, name: string): Promise<void> {
  await onAlarm(deps, name);
  if (name === UPLOAD_ALARM) await onUploadDue(deps);
  if (name === RETRY_ALARM) {
    await flushPendingStops(deps);
    await flushPendingRevocation(deps);
  }
}

/**
 * Browser start. The session left over from before is ended locally; its stop reaches the server
 * only after the queue is uploaded (session/controller.ts reportStop), so events recorded before
 * the restart are still accepted.
 */
export async function handleStartup(deps: Deps): Promise<void> {
  await flushPendingRevocation(deps); // a revocation first: it purges the queue
  await onStartup(deps);
  await flushPendingStops(deps);
  await resumeUpload(deps);
}

/**
 * The single choke point for Reel events. Nothing may reach the upload queue except through
 * emitEvent (test/gate.test.ts fails if another file calls enqueueEvent).
 *
 * Spec F1: with consent absent or revoked the extension emits ZERO events. Besides consent the gate
 * needs a running session that no automatic rule has already ended, so an event cannot slip out in
 * the gap before an alarm fires.
 */

import { isConsentActive } from '../consent/store';
import { enqueueEvent, type QueueDb } from '../queue/db';
import type { Store } from '../runtime';
import { readSession, stopReasonDue } from '../session/state';

export interface EmitDeps {
  store: Store;
  queue: QueueDb;
  now(): number;
}

export interface OutgoingEvent {
  event_id: string;
  body: Record<string, unknown>;
}

/** Queue one event for upload. Returns false, and writes nothing, when the gate is closed. */
export async function emitEvent(deps: EmitDeps, event: OutgoingEvent): Promise<boolean> {
  if (!(await isConsentActive(deps.store))) return false;

  const session = await readSession(deps.store);
  if (session === null) return false;
  if (stopReasonDue(session, deps.now()) !== null) return false;

  await enqueueEvent(deps.queue, {
    event_id: event.event_id,
    enqueued_at: new Date(deps.now()).toISOString(),
    body: event.body,
  });
  return true;
}

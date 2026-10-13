import { openDB, type DBSchema, type IDBPDatabase } from 'idb';

/**
 * Durable event queue in the extension origin (F8). Keyed by event_id, so writing the same
 * event twice never creates a duplicate. Upload, batching and backoff: upload/scheduler.ts.
 */
export interface QueuedEvent {
  event_id: string;
  enqueued_at: string;
  body: Record<string, unknown>;
}

interface QueueSchema extends DBSchema {
  events: { key: string; value: QueuedEvent };
}

export type QueueDb = IDBPDatabase<QueueSchema>;

export const QUEUE_DB_NAME = 'ref-queue';
export const QUEUE_DB_VERSION = 1;

export function openQueueDb(name: string = QUEUE_DB_NAME): Promise<QueueDb> {
  return openDB<QueueSchema>(name, QUEUE_DB_VERSION, {
    upgrade(db) {
      db.createObjectStore('events', { keyPath: 'event_id' });
    },
  });
}

export async function enqueueEvent(db: QueueDb, event: QueuedEvent): Promise<void> {
  await db.put('events', event);
}

export function countEvents(db: QueueDb): Promise<number> {
  return db.count('events');
}

export async function removeEvents(db: QueueDb, ids: readonly string[]): Promise<void> {
  const tx = db.transaction('events', 'readwrite');
  await Promise.all([...ids.map((id) => tx.store.delete(id)), tx.done]);
}

/** Delete every queued event. Used when consent is revoked: nothing unsent may remain. */
export async function clearEvents(db: QueueDb): Promise<void> {
  await db.clear('events');
}

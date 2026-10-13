import { describe, expect, it } from 'vitest';

import { countEvents, enqueueEvent, openQueueDb, removeEvents } from '../src/queue/db';

let n = 0;
const freshName = () => `test-queue-${++n}`;
const event = (id: string) => ({ event_id: id, enqueued_at: '2026-10-07T00:00:00Z', body: {} });

describe('event queue', () => {
  it('starts empty', async () => {
    expect(await countEvents(await openQueueDb(freshName()))).toBe(0);
  });

  it('is idempotent by event_id', async () => {
    const db = await openQueueDb(freshName());
    await enqueueEvent(db, event('a'));
    await enqueueEvent(db, event('a'));
    expect(await countEvents(db)).toBe(1);
  });

  it('survives reopening the database', async () => {
    const name = freshName();
    const first = await openQueueDb(name);
    await enqueueEvent(first, event('a'));
    first.close();
    expect(await countEvents(await openQueueDb(name))).toBe(1);
  });

  it('removes only the acknowledged events', async () => {
    const db = await openQueueDb(freshName());
    await enqueueEvent(db, event('a'));
    await enqueueEvent(db, event('b'));
    await removeEvents(db, ['a']);
    expect(await db.getAllKeys('events')).toEqual(['b']);
  });
});

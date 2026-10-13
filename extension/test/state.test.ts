import { describe, expect, it } from 'vitest';

import {
  readPendingStops,
  readSession,
  stopReasonDue,
  writePendingStops,
  writeSession,
  type ActiveSession,
} from '../src/session/state';
import { MINUTE, START, memoryStore } from './helpers';

const session = (over: Partial<ActiveSession> = {}): ActiveSession => ({
  sessionId: 's1',
  tabId: 7,
  startedAt: START,
  leftSurfaceAt: null,
  ...over,
});

describe('stopReasonDue', () => {
  it('is null for a fresh session', () => {
    expect(stopReasonDue(session(), START)).toBeNull();
    expect(stopReasonDue(session(), START + 59 * MINUTE + 59_000)).toBeNull();
  });

  it('is time_cap at 60 minutes', () => {
    expect(stopReasonDue(session(), START + 60 * MINUTE)).toBe('time_cap');
    expect(stopReasonDue(session(), START + 61 * MINUTE)).toBe('time_cap');
  });

  it('is left_surface 60 seconds after leaving, not before', () => {
    const left = session({ leftSurfaceAt: START + 5 * MINUTE });
    expect(stopReasonDue(left, START + 5 * MINUTE + 59_999)).toBeNull();
    expect(stopReasonDue(left, START + 5 * MINUTE + 60_000)).toBe('left_surface');
  });

  it('prefers time_cap when both are due', () => {
    const both = session({ leftSurfaceAt: START + 59 * MINUTE - 1 });
    expect(stopReasonDue(both, START + 61 * MINUTE)).toBe('time_cap');
  });
});

describe('persisted state', () => {
  it('round-trips a session and ignores corrupted values', async () => {
    const store = memoryStore();
    expect(await readSession(store)).toBeNull();
    await writeSession(store, session());
    expect(await readSession(store)).toEqual(session());
    store.data['activeSession'] = { sessionId: 1 };
    expect(await readSession(store)).toBeNull();
    store.data['activeSession'] = { ...session(), leftSurfaceAt: 'x' };
    expect(await readSession(store)).toBeNull();
  });

  it('keeps and drops pending stops', async () => {
    const store = memoryStore();
    await writePendingStops(store, [{ sessionId: 'a', reason: 'user' }]);
    expect(await readPendingStops(store)).toEqual([{ sessionId: 'a', reason: 'user' }]);
    await writePendingStops(store, []);
    expect(await readPendingStops(store)).toEqual([]);
    store.data['pendingStops'] = [{ nope: 1 }, 'x'];
    expect(await readPendingStops(store)).toEqual([]);
  });
});

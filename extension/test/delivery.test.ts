/**
 * Delivery acceptance test (spec F8, N5, roadmap M2 exit): no event loss and no duplicates after a
 * simulated 2 minute network outage.
 *
 * Real extension code end to end: content-script messages into the service worker handlers, the
 * IndexedDB queue (fake-indexeddb), the upload scheduler, session start and stop, browser restart.
 * Only the network and the clock are simulated. The fake server applies the same rules as
 * server/app/routers/events.py: per-event results, dedupe by event_id, refusal of events for a
 * stopped or unknown session. The server half of the check (real database, replays) is
 * tests/test_events.py::test_retries_after_a_two_minute_outage_lose_nothing_and_duplicate_nothing.
 *
 * The clock moves in 1 second steps. Chrome fires an alarm no sooner than 30 s after it was set,
 * and the in-memory 15 s timer is lost when the worker restarts; both are modelled.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { ApiError, type EventResult, type SessionDto, type StopReason } from '../src/api/client';
import { handleAlarm, handleRequest, handleStartup, routeMessage } from '../src/background/handlers';
import { countEvents } from '../src/queue/db';
import type { Deps } from '../src/runtime';
import { readPendingStops } from '../src/session/state';
import { UPLOAD_ALARM, readDropped } from '../src/upload/scheduler';
import { REELS_URL, makeEnv, validBody, type Env } from './helpers';

const OWN = 'ext-id';
const SECOND = 1_000;
const MINUTE = 60 * SECOND;

/** What the server holds, and how the network between it and the extension behaves. */
class FakeServer {
  online = true;
  /** The request reaches the server and is processed, but the reply is lost. */
  dropReplies = false;
  readonly sessions = new Map<string, { status: 'active' | 'stopped'; reason: StopReason | null }>();
  readonly rows = new Map<string, string>(); // event_id -> session_id
  readonly jobs: string[] = [];
  requests = 0;
  failedRequests = 0;
  private next = 0;

  constructor(private readonly now: () => number) {}

  private wire(): void {
    this.requests += 1;
    if (!this.online) {
      this.failedRequests += 1;
      throw new ApiError(0, 'network');
    }
  }

  private reply<T>(value: T): T {
    if (this.dropReplies) {
      this.failedRequests += 1;
      throw new ApiError(0, 'network');
    }
    return value;
  }

  startSession = (): Promise<SessionDto> => {
    this.wire();
    for (const s of this.sessions.values()) {
      if (s.status === 'active') Object.assign(s, { status: 'stopped', reason: 'browser_restart' });
    }
    const id = `session-${++this.next}`;
    this.sessions.set(id, { status: 'active', reason: null });
    return Promise.resolve(
      this.reply({
        id,
        started_at: new Date(this.now()).toISOString(),
        ended_at: null,
        status: 'active',
        stop_reason: null,
      }),
    );
  };

  stopSession = (id: string, reason: StopReason): Promise<SessionDto> => {
    this.wire();
    const s = this.sessions.get(id);
    if (s === undefined) return Promise.reject(new ApiError(404, 'Session not found'));
    if (s.status === 'active') Object.assign(s, { status: 'stopped', reason });
    return Promise.resolve(
      this.reply({ id, started_at: '', ended_at: '', status: 'stopped', stop_reason: s.reason }),
    );
  };

  postEvents = (events: readonly { event_id: string; body: Record<string, unknown> }[]) => {
    this.wire();
    const results: EventResult[] = events.map(({ event_id, body }) => {
      const sessionId = body['session_id'];
      const session = typeof sessionId === 'string' ? this.sessions.get(sessionId) : undefined;
      if (session === undefined) return { event_id, status: 'rejected', reason: 'session_not_found' };
      if (session.status !== 'active') {
        return { event_id, status: 'rejected', reason: 'session_inactive' };
      }
      if (this.rows.has(event_id)) return { event_id, status: 'duplicate', reason: null };
      this.rows.set(event_id, sessionId as string);
      this.jobs.push(event_id);
      return { event_id, status: 'stored', reason: null };
    });
    return Promise.resolve(this.reply(results));
  };
}

/** One browser profile: storage and queue persist; the worker's memory does not survive a restart. */
class Browser {
  readonly alarms = new Map<string, number>();
  wakeAt: number | null = null;
  deps: Deps;
  readonly emitted: string[] = [];
  private seq = 0;

  constructor(
    readonly env: Env,
    readonly server: FakeServer,
  ) {
    this.deps = this.worker();
  }

  /** A fresh service worker: same storage and queue, no in-memory timer, no in-flight flush. */
  private worker(): Deps {
    return {
      ...this.env.deps,
      alarms: {
        // Chrome: an alarm never fires sooner than 30 s after it is set. Same name replaces.
        create: (name, when) => void this.alarms.set(name, Math.max(when, this.env.now() + 30 * SECOND)),
        clear: (name) => void this.alarms.delete(name),
      },
      wakeAfter: (ms) => void (this.wakeAt = this.env.now() + ms),
    };
  }

  restart(): Promise<void> {
    this.wakeAt = null;
    this.deps = this.worker();
    return handleStartup(this.deps);
  }

  start(): Promise<unknown> {
    return handleRequest(this.deps, { type: 'startSession' });
  }

  stop(): Promise<unknown> {
    return handleRequest(this.deps, { type: 'stopSession' });
  }

  /** The content script reports one finished viewing. */
  async view(flush = false): Promise<void> {
    const event_id = crypto.randomUUID();
    const body = validBody({
      reel_shortcode: `Creel${String(++this.seq).padStart(4, '0')}`,
      observed_at: new Date(this.env.now()).toISOString(),
    });
    const reply = await routeMessage(
      this.deps,
      OWN,
      { type: 'reelEvent', event_id, body, ...(flush ? { flush } : {}) },
      { id: OWN, tabId: 7, tabUrl: REELS_URL },
    );
    expect(reply).toEqual({ accepted: true });
    this.emitted.push(event_id);
  }

  /** Let `ms` pass in 1 s steps, firing due timers and alarms; optionally view a Reel every `every`. */
  async run(ms: number, every?: number): Promise<void> {
    for (let t = 0; t < ms; t += SECOND) {
      this.env.advance(SECOND);
      if (every !== undefined && (t + SECOND) % every === 0) await this.view();
      if (this.wakeAt !== null && this.wakeAt <= this.env.now()) {
        this.wakeAt = null;
        await handleAlarm(this.deps, UPLOAD_ALARM);
      }
      for (const [name, when] of [...this.alarms]) {
        if (when <= this.env.now()) {
          this.alarms.delete(name);
          await handleAlarm(this.deps, name);
        }
      }
    }
  }

  async queued(): Promise<number> {
    const db = await this.deps.openQueue();
    try {
      return await countEvents(db);
    } finally {
      db.close();
    }
  }

  /** Every viewing is stored exactly once, with one job, and nothing is left or dropped. */
  async expectAllDelivered(): Promise<void> {
    expect(new Set(this.emitted).size).toBe(this.emitted.length);
    expect([...this.server.rows.keys()].sort()).toEqual([...this.emitted].sort());
    expect([...this.server.jobs].sort()).toEqual([...this.emitted].sort());
    expect(await this.queued()).toBe(0);
    expect(await readDropped(this.deps)).toEqual({});
    expect(await readPendingStops(this.env.store)).toEqual([]);
  }
}

let browser: Browser;
let server: FakeServer;

beforeEach(async () => {
  const env = makeEnv();
  server = new FakeServer(env.now);
  env.api.startSession.mockImplementation(server.startSession);
  env.api.stopSession.mockImplementation(server.stopSession);
  env.api.postEvents.mockImplementation(server.postEvents);
  await env.ready();
  browser = new Browser(env, server);
  expect(await browser.start()).toEqual({ ok: true });
});

const VIEW_EVERY = 4 * SECOND; // a Reel every 4 seconds: 30 events in a 2 minute outage

describe('a 2 minute network outage', () => {
  it('in the middle of a session: nothing lost, nothing duplicated, sent soon after', async () => {
    await browser.run(2 * MINUTE, VIEW_EVERY);
    const beforeOutage = browser.emitted.length;
    expect(server.rows.size).toBeGreaterThan(beforeOutage - 10); // uploads were flowing

    server.online = false;
    await browser.run(2 * MINUTE, VIEW_EVERY);
    expect(server.failedRequests).toBeGreaterThan(0);
    expect(server.failedRequests).toBeLessThanOrEqual(5); // backoff, not one request per event
    expect(await browser.queued()).toBeGreaterThanOrEqual(30);

    server.online = true;
    await browser.run(2 * MINUTE, VIEW_EVERY);
    // Everything viewed during the outage is on the server within 2 minutes of it ending.
    const outageEvents = browser.emitted.slice(beforeOutage, beforeOutage + 30);
    expect(outageEvents.every((id) => server.rows.has(id))).toBe(true);

    await browser.stop();
    await browser.run(1 * MINUTE);
    await browser.expectAllDelivered();
    expect(server.sessions.get('session-1')).toEqual({ status: 'stopped', reason: 'user' });
  });

  it('with replies lost (the server stored the batch, the extension never heard): no duplicates', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.dropReplies = true;
    await browser.run(2 * MINUTE, VIEW_EVERY);
    expect(server.rows.size).toBeGreaterThan(0); // batches did arrive during the "outage"
    server.dropReplies = false;
    await browser.run(3 * MINUTE, VIEW_EVERY);
    await browser.stop();
    await browser.run(1 * MINUTE);
    await browser.expectAllDelivered();
  });

  it('the user presses Stop during the outage: the events are still accepted afterwards', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.online = false;
    await browser.run(1 * MINUTE, VIEW_EVERY);
    await browser.stop(); // offline: local stop now, the server is told after the events
    await browser.run(1 * MINUTE);
    expect(server.sessions.get('session-1')?.status).toBe('active');

    server.online = true;
    await browser.run(3 * MINUTE);
    await browser.expectAllDelivered();
    expect(server.sessions.get('session-1')).toEqual({ status: 'stopped', reason: 'user' });
  });

  it('the browser restarts during the outage: the queue survives and is sent afterwards', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.online = false;
    await browser.run(1 * MINUTE, VIEW_EVERY);
    const held = await browser.queued();
    expect(held).toBeGreaterThan(0);

    await browser.restart(); // session ends locally (browser_restart); queue is on disk
    expect(await browser.queued()).toBe(held);
    await browser.run(1 * MINUTE);

    server.online = true;
    await browser.run(4 * MINUTE);
    await browser.expectAllDelivered();
    expect(server.sessions.get('session-1')).toEqual({ status: 'stopped', reason: 'browser_restart' });
  });

  it('the browser restarts after the outage ended, before the retry fired', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.online = false;
    await browser.run(2 * MINUTE, VIEW_EVERY);
    server.online = true;
    await browser.restart(); // alarms from before are gone too
    browser.alarms.clear();
    await browser.run(1 * MINUTE);
    await browser.expectAllDelivered();
  });

  it('the page goes away at the end of the outage: the pagehide event is sent with the rest', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.online = false;
    await browser.run(2 * MINUTE, VIEW_EVERY);
    server.online = true;
    await browser.view(true); // pagehide: upload now, which also takes the backlog
    expect(await browser.queued()).toBe(0);
    await browser.stop();
    await browser.expectAllDelivered();
  });

  it('a new session started after an outage still gets the old session\'s events in', async () => {
    await browser.run(1 * MINUTE, VIEW_EVERY);
    server.online = false;
    await browser.run(1 * MINUTE, VIEW_EVERY);
    await browser.stop();
    server.online = true;
    // Start again before any retry fired: the old events and the old stop go out first.
    expect(await browser.start()).toEqual({ ok: true });
    await browser.run(1 * MINUTE, VIEW_EVERY);
    await browser.stop();
    await browser.run(1 * MINUTE);
    await browser.expectAllDelivered();
    expect(server.sessions.get('session-1')).toEqual({ status: 'stopped', reason: 'user' });
  });
});

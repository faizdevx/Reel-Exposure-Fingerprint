import { vi } from 'vitest';

import { openQueueDb } from '../src/queue/db';
import type { ApiClient, ConsentDto, SessionDto } from '../src/api/client';
import { TOKEN_KEY } from '../src/auth/token';
import { POLICY_VERSION } from '../src/consent/policy';
import { writeConsent } from '../src/consent/store';
import type { Deps, Store, TabInfo } from '../src/runtime';

import type { ExposureBody } from '../src/shared/exposure';

/** A complete, valid exposure event body, as the content script sends it. */
export function validBody(over: Partial<ExposureBody> = {}): ExposureBody {
  return {
    reel_shortcode: 'Cabc123',
    observed_at: '2026-10-07T12:00:05.000Z',
    dwell_ms: 8420,
    video_duration_ms: 15000,
    video_duration_status: 'available',
    creator_handle: { value: 'somecreator', status: 'available' },
    caption: { value: 'text', status: 'available' },
    caption_truncated: false,
    hashtags: { value: ['ai'], status: 'available' },
    frame_count: 0,
    adapter_version: 'ig-web-2026.10.1',
    ...over,
  };
}

export const TOKEN = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCde'; // 43 URL-safe characters
export const REELS_URL = 'https://www.instagram.com/reels/Cabc123/';
export const START = Date.UTC(2026, 9, 7, 12, 0, 0);
export const MINUTE = 60_000;

export function memoryStore(): Store & { data: Record<string, unknown> } {
  const data: Record<string, unknown> = {};
  return {
    data,
    get: (keys) =>
      Promise.resolve(Object.fromEntries(keys.filter((k) => k in data).map((k) => [k, data[k]]))),
    set: (items) => {
      Object.assign(data, structuredClone(items));
      return Promise.resolve();
    },
    remove: (keys) => {
      for (const k of keys) Reflect.deleteProperty(data, k);
      return Promise.resolve();
    },
  };
}

export type FakeApi = { [K in keyof ApiClient]: ReturnType<typeof vi.fn<ApiClient[K]>> };

/** A complete fake browser: storage, alarms, clock, tabs, badge, queue purge and API. */
let envCounter = 0;

export function makeEnv() {
  const store = memoryStore();
  const alarms = new Map<string, number>();
  const badge: boolean[] = [];
  let time = START;
  let active: TabInfo = { id: 7, url: REELS_URL };
  const tabUrls = new Map<number, string | undefined>([[7, REELS_URL]]);
  let purges = 0;
  const notified: { tabId: number; active: boolean }[] = [];
  const wakes: number[] = [];
  const queueName = `env-queue-${++envCounter}`;

  const iso = () => new Date(time).toISOString();
  const consentDto = (): ConsentDto => ({
    policy_version: POLICY_VERSION,
    granted_at: iso(),
    revoked_at: null,
  });
  const sessionDto = (): SessionDto => ({
    id: 'session-1',
    started_at: iso(),
    ended_at: null,
    status: 'active',
    stop_reason: null,
  });

  const api: FakeApi = {
    putConsent: vi.fn<ApiClient['putConsent']>(() => Promise.resolve(consentDto())),
    deleteConsent: vi.fn<ApiClient['deleteConsent']>(() => Promise.resolve()),
    startSession: vi.fn<ApiClient['startSession']>(() => Promise.resolve(sessionDto())),
    stopSession: vi.fn<ApiClient['stopSession']>(() =>
      Promise.resolve({ ...sessionDto(), status: 'stopped', ended_at: iso(), stop_reason: 'user' }),
    ),
    postEvents: vi.fn<ApiClient['postEvents']>((events) =>
      Promise.resolve(events.map((e) => ({ event_id: e.event_id, status: 'stored', reason: null }))),
    ),
  };

  const deps: Deps = {
    store,
    api,
    alarms: {
      create: (name, whenMs) => void alarms.set(name, whenMs),
      clear: (name) => void alarms.delete(name),
    },
    setBadge: (on) => void badge.push(on),
    now: () => time,
    getTabUrl: (tabId) => Promise.resolve(tabUrls.get(tabId)),
    getActiveTab: () => Promise.resolve(active),
    purgeQueue: async () => {
      purges += 1;
      const db = await openQueueDb(queueName);
      await db.clear('events');
      db.close();
    },
    openQueue: () => openQueueDb(queueName),
    notifyContent: (tabId, active) => void notified.push({ tabId, active }),
    wakeAfter: (ms) => void wakes.push(ms),
  };

  return {
    deps,
    store,
    api,
    alarms,
    badge,
    purgeCount: () => purges,
    notified,
    wakes,
    advance: (ms: number) => void (time += ms),
    now: () => time,
    setActiveTab: (tab: TabInfo) => void (active = tab),
    setTabUrl: (tabId: number, url: string | undefined) => void tabUrls.set(tabId, url),
    async consent() {
      await writeConsent(store, { policyVersion: POLICY_VERSION, grantedAt: iso() });
    },
    token() {
      store.data[TOKEN_KEY] = TOKEN;
    },
    /** The state a user reaches before pressing Start. */
    async ready() {
      await this.consent();
      this.token();
    },
  };
}

export type Env = ReturnType<typeof makeEnv>;

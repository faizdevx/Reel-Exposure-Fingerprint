import type { ApiClient } from './api/client';
import type { TokenStorage } from './auth/token';
import type { QueueDb } from './queue/db';

/** chrome.storage.local, or any stand-in with the same three calls. */
export type Store = TokenStorage;

export interface Alarms {
  /** One-shot alarm at an absolute time in milliseconds. */
  create(name: string, whenMs: number): unknown;
  clear(name: string): unknown;
}

export interface TabInfo {
  id: number | undefined;
  url: string | undefined;
}

/**
 * Everything the extension logic needs from the browser, injected so the rules can be tested
 * without Chrome. background/index.ts is the only place that builds the real thing.
 */
export interface Deps {
  store: Store;
  api: ApiClient;
  alarms: Alarms;
  setBadge(on: boolean): unknown;
  now(): number;
  /** URL of a tab, or undefined when the browser hides it (not an instagram.com page). */
  getTabUrl(tabId: number): Promise<string | undefined>;
  getActiveTab(): Promise<TabInfo | undefined>;
  /** Delete every event queued for upload. */
  purgeQueue(): Promise<void>;
  /** Open the extension-origin IndexedDB queue. The caller closes it. */
  openQueue(): Promise<QueueDb>;
  /** Tell a tab's content script whether monitoring is on there. Failures are ignored. */
  notifyContent(tabId: number, active: boolean): unknown;
  /**
   * Best-effort in-memory timer for a wait shorter than Chrome's 30 s alarm minimum: after `ms`,
   * run the upload check (upload/scheduler.ts onUploadDue). Replaces any earlier timer. Lost if the
   * worker is suspended; the upload alarm is the durable backstop.
   */
  wakeAfter(ms: number): void;
}

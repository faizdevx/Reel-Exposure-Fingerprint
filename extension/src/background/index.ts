import { createApiClient } from '../api/client';
import { readToken } from '../auth/token';
import { API_BASE } from '../config';
import { clearEvents, openQueueDb } from '../queue/db';
import type { Deps } from '../runtime';
import { onTabRemoved, onTabUpdated } from '../session/controller';
import { UPLOAD_ALARM, resumeUpload } from '../upload/scheduler';
import { handleAlarm, handleStartup, routeMessage } from './handlers';

// The only place that touches chrome.* for session and consent logic. Everything it calls takes
// `deps`, so the rules are tested without a browser.
const store = chrome.storage.local;
let uploadTimer: ReturnType<typeof setTimeout> | undefined;

const deps: Deps = {
  store,
  api: createApiClient({ baseUrl: API_BASE, getToken: () => readToken(store) }),
  alarms: {
    create: (name, whenMs) => chrome.alarms.create(name, { when: whenMs }),
    clear: (name) => chrome.alarms.clear(name),
  },
  async setBadge(on) {
    await chrome.action.setBadgeText({ text: on ? 'REC' : '' });
    if (on) await chrome.action.setBadgeBackgroundColor({ color: '#c62828' });
  },
  now: () => Date.now(),
  async getTabUrl(tabId) {
    try {
      return (await chrome.tabs.get(tabId)).url; // undefined unless the tab is on instagram.com
    } catch {
      return undefined; // the tab is gone
    }
  },
  async getActiveTab() {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return tab ? { id: tab.id, url: tab.url } : undefined;
  },
  openQueue: () => openQueueDb(),
  async notifyContent(tabId, active) {
    try {
      await chrome.tabs.sendMessage(tabId, { type: 'monitoringState', active });
    } catch {
      // no content script in that tab (not loaded yet, or the tab is gone): it asks when it loads
    }
  },
  wakeAfter(ms) {
    clearTimeout(uploadTimer);
    uploadTimer = setTimeout(() => void handleAlarm(deps, UPLOAD_ALARM), ms);
  },
  async purgeQueue() {
    const db = await openQueueDb();
    try {
      await clearEvents(db);
    } finally {
      db.close();
    }
  },
};

// Listeners must be registered synchronously: the worker can be woken by any of them.
chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
  const reply = routeMessage(deps, chrome.runtime.id, message, {
    id: sender.id,
    tabId: sender.tab?.id,
    tabUrl: sender.tab?.url,
  });
  if (reply === null) return false;
  void reply.then(sendResponse);
  return true; // keep the channel open for the async response
});
chrome.tabs.onRemoved.addListener((tabId) => void onTabRemoved(deps, tabId));
chrome.tabs.onUpdated.addListener((tabId) => void onTabUpdated(deps, tabId));
chrome.alarms.onAlarm.addListener((alarm) => void handleAlarm(deps, alarm.name));
chrome.runtime.onStartup.addListener(() => void handleStartup(deps));
// After an extension update or reload the queue is still there; its alarm may not be.
chrome.runtime.onInstalled.addListener(() => void resumeUpload(deps));

import { createExposureTracker } from '../exposure/tracker';
import { browserEnv } from '../ig-adapter';
import { createContent } from './controller';

const adapterEnv = browserEnv();
// True while pagehide is ending the open viewing: that event asks the service worker to upload now.
let leaving = false;

const exposure = createExposureTracker({
  env: adapterEnv,
  now: () => performance.now(),
  wallNow: () => Date.now(),
  newId: () => crypto.randomUUID(),
  emit(event) {
    // Fire and forget: the service worker decides whether the event may be queued.
    const message = leaving ? { type: 'reelEvent', ...event, flush: true } : { type: 'reelEvent', ...event };
    chrome.runtime.sendMessage(message).catch(() => undefined);
  },
  schedule(fn, ms) {
    const id = setTimeout(fn, ms);
    return () => clearTimeout(id);
  },
});

const content = createContent({
  doc: document,
  exposure,
  href: () => location.href,
  send: (request) => chrome.runtime.sendMessage(request),
});

chrome.runtime.onMessage.addListener((message: unknown, sender) => {
  // Only the service worker of this extension may push state.
  if (sender.id === chrome.runtime.id && sender.tab === undefined) content.onPush(message);
  return false;
});

// pagehide also fires when the page enters the back/forward cache; pageshow(persisted) undoes it.
addEventListener('pagehide', () => {
  leaving = true; // the tracker emits synchronously inside onPageHide
  try {
    content.onPageHide();
  } finally {
    leaving = false;
  }
});
addEventListener('pageshow', (event) => content.onPageShow(event.persisted));

void content.start();

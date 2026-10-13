/**
 * Content script logic. It runs on instagram.com, so it is deliberately tiny:
 * - It does nothing unless the page address is the Reels surface (spec F3). Elsewhere it sends no
 *   message and touches no DOM, even if told monitoring is on.
 * - It owns no storage and no queue. Whether to show the marker and track Reels comes from the
 *   service worker; finished viewings go back to it as messages (exposure/tracker.ts).
 * - It never reads cookies or credentials, makes no network request, and does not scroll, click
 *   or navigate. Reel detection goes through ig-adapter.ts only.
 */

import { isContentPush, type ContentRequest, type ContentState } from '../shared/messages';
import { isReelsSurface } from '../session/surface';
import { hideMarker, showMarker } from './marker';

/** What the content script needs from the exposure tracker. */
export interface ExposureControl {
  start(): void;
  /** Ends the open viewing (sending it) and stops watching. */
  stop(): void;
  pageHide(): void;
  resume(): void;
}

export interface ContentEnv {
  doc: Document;
  /** Absent in tests that only cover the marker. */
  exposure?: ExposureControl;
  /** Current page address. Read again on every call: Instagram navigates without reloading. */
  href(): string;
  send(request: ContentRequest): Promise<unknown>;
}

export interface Content {
  /** A message from the service worker. Returns true when it was a valid monitoringState push. */
  onPush(message: unknown): boolean;
  /** Ask the service worker once, when the script loads. */
  start(): Promise<void>;
  /** The page is being hidden or unloaded: finish the open viewing once. */
  onPageHide(): void;
  /** The page was restored from the back/forward cache. */
  onPageShow(persisted: boolean): void;
}

export function createContent(env: ContentEnv): Content {
  let tracking = false;
  const apply = (active: boolean) => {
    if (active && isReelsSurface(env.href())) {
      showMarker(env.doc);
      tracking = true;
      env.exposure?.start();
    } else {
      hideMarker(env.doc);
      tracking = false;
      env.exposure?.stop();
    }
  };

  return {
    onPush(message) {
      if (!isContentPush(message)) return false;
      apply(message.active);
      return true;
    },
    async start() {
      if (!isReelsSurface(env.href())) return; // idle: no message, no DOM
      let reply: unknown;
      try {
        reply = await env.send({ type: 'contentHello' });
      } catch {
        return; // the service worker is not reachable: stay hidden and idle
      }
      const state = reply as Partial<ContentState> | null;
      apply(state?.active === true);
    },
    onPageHide() {
      env.exposure?.pageHide();
    },
    onPageShow(persisted) {
      // Only a page restored from the cache needs this; a fresh load asks the service worker.
      if (persisted && tracking && isReelsSurface(env.href())) env.exposure?.resume();
    },
  };
}

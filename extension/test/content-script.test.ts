import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createContent } from '../src/content/controller';
import { MARKER_ID } from '../src/content/marker';
import type { ContentRequest } from '../src/shared/messages';
import { REELS_URL } from './helpers';

let href: string;
let send: ReturnType<typeof vi.fn<(r: ContentRequest) => Promise<unknown>>>;
const marker = () => document.getElementById(MARKER_ID);
const make = () => createContent({ doc: document, href: () => href, send });
const on = { type: 'monitoringState', active: true };
const off = { type: 'monitoringState', active: false };

beforeEach(() => {
  document.documentElement.replaceChildren(
    document.createElement('head'),
    document.createElement('body'),
  );
  href = REELS_URL;
  send = vi.fn<(r: ContentRequest) => Promise<unknown>>(() => Promise.resolve({ active: true }));
});

describe('on the Reels surface', () => {
  it('asks the service worker once and shows the marker when active', async () => {
    await make().start();
    expect(send).toHaveBeenCalledExactlyOnceWith({ type: 'contentHello' });
    expect(marker()).not.toBeNull();
  });

  it('stays hidden when the service worker says inactive', async () => {
    send.mockResolvedValue({ active: false });
    await make().start();
    expect(marker()).toBeNull();
  });

  it('stays hidden when the service worker is unreachable or answers garbage', async () => {
    send.mockRejectedValue(new Error('no receiver'));
    await make().start();
    expect(marker()).toBeNull();
    for (const reply of [undefined, null, 'yes', { active: 'true' }]) {
      send.mockResolvedValue(reply);
      await make().start();
      expect(marker()).toBeNull();
    }
  });

  it('follows pushes: show, show again without duplicating, hide', () => {
    const c = make();
    expect(c.onPush(on)).toBe(true);
    expect(c.onPush(on)).toBe(true);
    expect(document.querySelectorAll(`#${MARKER_ID}`)).toHaveLength(1);
    expect(c.onPush(off)).toBe(true);
    expect(marker()).toBeNull();
  });

  it('ignores messages that are not a valid push', () => {
    const c = make();
    expect(c.onPush({ type: 'monitoringState', active: 'yes' })).toBe(false);
    expect(c.onPush({ type: 'getStatus' })).toBe(false);
    expect(c.onPush(null)).toBe(false);
    expect(marker()).toBeNull();
  });

  it('hides the marker when Instagram navigates away without a reload', () => {
    const c = make();
    c.onPush(on);
    expect(marker()).not.toBeNull();
    href = 'https://www.instagram.com/';
    c.onPush(on); // a stale "on" push must not show it off the surface
    expect(marker()).toBeNull();
  });

  it('marker is a shadow-root element that does not catch clicks', () => {
    make().onPush(on);
    const root = marker()?.shadowRoot;
    expect(root).not.toBeNull();
    expect(root?.querySelector('style')?.textContent).toContain('pointer-events: none');
    expect(root?.textContent).toContain('monitoring is on');
  });
});

describe('outside the Reels surface: idle', () => {
  const others = [
    'https://www.instagram.com/',
    'https://www.instagram.com/explore/',
    'https://www.instagram.com/p/abc123/',
    'https://www.instagram.com/reel/abc123/',
    'https://www.instagram.com/someone/reels/',
    'https://www.instagram.com.evil.example/reels/abc/',
    'http://www.instagram.com/reels/abc/',
  ];

  it.each(others)('sends nothing and shows nothing on %s', async (url) => {
    href = url;
    const before = document.documentElement.innerHTML;
    const c = make();
    await c.start();
    c.onPush(on);
    expect(send).not.toHaveBeenCalled();
    expect(marker()).toBeNull();
    expect(document.documentElement.innerHTML).toBe(before);
  });
});

describe('the content script owns no storage and does no automation', () => {
  const dir = join(__dirname, '..', 'src', 'content');
  const files = readdirSync(dir).filter((f) => f.endsWith('.ts'));
  const forbidden = [
    /indexedDB/,
    /from 'idb'/,
    /\bopenDB\b/,
    /chrome\.storage/,
    /localStorage|sessionStorage/,
    /queue\//,
    /auth\/token/,
    /api\/client/,
    /\bfetch\s*\(/,
    /XMLHttpRequest/,
    /cookie/i,
    /\.(click|scrollTo|scrollBy|scrollIntoView|dispatchEvent|play|submit)\s*\(/,
    /new (MouseEvent|KeyboardEvent|WheelEvent|TouchEvent)/,
    /location\.(assign|replace)|location\.href\s*=/,
    /history\.(pushState|replaceState)/,
  ];

  it('has content files to scan', () => {
    expect(files).toEqual(expect.arrayContaining(['controller.ts', 'index.ts', 'marker.ts']));
  });

  it.each(files)('%s uses none of the forbidden APIs', (file) => {
    const source = readFileSync(join(dir, file), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '') // comments may mention what the file does not do
      .replace(/^\s*\/\/.*$/gm, '');
    for (const pattern of forbidden) {
      expect(source, `${file} matches ${String(pattern)}`).not.toMatch(pattern);
    }
  });
});

describe('exposure tracking follows the service worker', () => {
  const makeExposure = () => ({
    start: vi.fn<() => void>(),
    stop: vi.fn<() => void>(),
    pageHide: vi.fn<() => void>(),
    resume: vi.fn<() => void>(),
  });
  const withExposure = (exposure: ReturnType<typeof makeExposure>) =>
    createContent({ doc: document, href: () => href, send, exposure });

  it('starts tracking when monitoring is on, and stops (sending the open viewing) when it ends', async () => {
    const exposure = makeExposure();
    const c = withExposure(exposure);
    await c.start();
    expect(exposure.start).toHaveBeenCalledOnce();
    c.onPush(off);
    expect(exposure.stop).toHaveBeenCalledOnce();
  });

  it('never starts tracking outside the Reels surface, even when told monitoring is on', async () => {
    const exposure = makeExposure();
    href = 'https://www.instagram.com/';
    const c = withExposure(exposure);
    await c.start();
    c.onPush(on);
    expect(exposure.start).not.toHaveBeenCalled();
  });

  it('stops tracking when Instagram navigates away while a stale on push arrives', () => {
    const exposure = makeExposure();
    const c = withExposure(exposure);
    c.onPush(on);
    href = 'https://www.instagram.com/explore/';
    c.onPush(on);
    expect(exposure.stop).toHaveBeenCalled();
  });

  it('pagehide reaches the tracker; pageshow resumes only a cached page that was tracking', () => {
    const exposure = makeExposure();
    const c = withExposure(exposure);
    c.onPageShow(true);
    expect(exposure.resume).not.toHaveBeenCalled(); // was never tracking
    c.onPush(on);
    c.onPageHide();
    expect(exposure.pageHide).toHaveBeenCalledOnce();
    c.onPageShow(false);
    expect(exposure.resume).not.toHaveBeenCalled(); // a fresh load asks the service worker
    c.onPageShow(true);
    expect(exposure.resume).toHaveBeenCalledOnce();
  });

  it('does not resume after monitoring was turned off', () => {
    const exposure = makeExposure();
    const c = withExposure(exposure);
    c.onPush(on);
    c.onPush(off);
    c.onPageShow(true);
    expect(exposure.resume).not.toHaveBeenCalled();
  });
});

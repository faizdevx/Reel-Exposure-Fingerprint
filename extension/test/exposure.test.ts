import { beforeEach, describe, expect, it } from 'vitest';

import { routeMessage } from '../src/background/handlers';
import {
  METADATA_REFRESH_MS,
  createExposureTracker,
  type ExposureEvent,
  type ExposureTracker,
} from '../src/exposure/tracker';
import { ADAPTER_VERSION, type AdapterEnv } from '../src/ig-adapter';
import { isExposureBody } from '../src/shared/exposure';
import { startSession } from '../src/session/controller';
import { countEvents } from '../src/queue/db';
import {
  FakeIO,
  FakeMO,
  fixture,
  load,
  setVideo,
  setVisibility,
  videoById,
} from './dom-fakes';
import { REELS_URL, makeEnv } from './helpers';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const WALL_START = Date.UTC(2026, 9, 7, 12, 0, 5);

let mono: number;
let wall: number;
let href: string;
let ids: number;
let events: ExposureEvent[];
let scheduled: { fn: () => void; ms: number; cancelled: boolean }[];
let emitThrows: boolean;
let tracker: ExposureTracker;

const adapterEnv = (): AdapterEnv => ({
  doc: document,
  href: () => href,
  IntersectionObserverCtor: FakeIO as never,
  MutationObserverCtor: FakeMO as never,
});

function makeTracker(): ExposureTracker {
  return createExposureTracker({
    env: adapterEnv(),
    now: () => mono,
    wallNow: () => wall,
    newId: () => `00000000-0000-4000-8000-${String(++ids).padStart(12, '0')}`,
    emit(event) {
      if (emitThrows) throw new Error('send failed');
      events.push(event);
    },
    schedule(fn, ms) {
      const entry = { fn, ms, cancelled: false };
      scheduled.push(entry);
      return () => {
        entry.cancelled = true;
      };
    },
  });
}

const advance = (ms: number) => {
  mono += ms;
  wall += ms;
};
const v1 = () => videoById('v1');
const v2 = () => videoById('v2');
const io = () => {
  if (FakeIO.last === null) throw new Error('tracker did not start');
  return FakeIO.last;
};
const play = (v: HTMLVideoElement) => {
  setVideo(v, { paused: false, ended: false, readyState: 4 });
  v.dispatchEvent(new Event('playing'));
};
const pause = (v: HTMLVideoElement) => {
  setVideo(v, { paused: true });
  v.dispatchEvent(new Event('pause'));
};
const runRefresh = () => {
  for (const s of scheduled.filter((x) => !x.cancelled)) {
    s.cancelled = true;
    s.fn();
  }
};

/** Video 1 is primary and playing. */
const startWatching = (video = v1()) => {
  play(video);
  tracker.start();
  io().fire([video, 1]);
};

beforeEach(() => {
  mono = 1000;
  wall = WALL_START;
  href = REELS_URL;
  ids = 0;
  events = [];
  scheduled = [];
  emitThrows = false;
  FakeIO.last = null;
  FakeMO.last = null;
  setVisibility('visible');
  load(fixture('feed-two-reels.html'));
  setVideo(v1(), { duration: 12 });
  setVideo(v2(), { duration: 20 });
  tracker = makeTracker();
});

describe('normal viewing', () => {
  it('emits one event with every field when the Reel stops being primary', () => {
    startWatching();
    advance(8420);
    expect(events).toEqual([]); // nothing until the viewing ends
    io().fire([v1(), 0.2]);

    expect(events).toHaveLength(1);
    const e = events[0];
    expect(e?.event_id).toMatch(UUID);
    expect(e?.body).toEqual({
      reel_shortcode: 'Aaaaa11111_',
      observed_at: new Date(WALL_START).toISOString(),
      dwell_ms: 8420,
      video_duration_ms: 12000,
      video_duration_status: 'available',
      creator_handle: { value: 'first_creator', status: 'available' },
      caption: { value: 'First caption #one', status: 'available' },
      caption_truncated: false,
      hashtags: { value: ['one'], status: 'available' },
      frame_count: 0,
      adapter_version: ADAPTER_VERSION,
    });
    expect(isExposureBody(e?.body)).toBe(true);
    expect(tracker.isOpen()).toBe(false);
  });

  it('observed_at is when the Reel became primary, not when it was left', () => {
    startWatching();
    advance(5000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.observed_at).toBe(new Date(WALL_START).toISOString());
  });

  it('a Reel that never reaches 60% visible is never a viewing', () => {
    tracker.start();
    io().fire([v1(), 0.59]);
    advance(5000);
    io().fire([v1(), 0.3]);
    expect(events).toEqual([]);
    expect(tracker.isOpen()).toBe(false);
  });

  it('a Reel that is primary but never played has dwell 0 and is still one exposure', () => {
    setVideo(v1(), { paused: true });
    tracker.start();
    io().fire([v1(), 1]);
    advance(6000);
    io().fire([v1(), 0]);
    expect(events).toHaveLength(1);
    expect(events[0]?.body.dwell_ms).toBe(0);
  });

  it('measures dwell on the monotonic clock: a wall-clock jump changes nothing', () => {
    startWatching();
    mono += 4000;
    wall += 3 * 60 * 60 * 1000; // the system clock jumps three hours
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(4000);
  });
});

describe('pause', () => {
  it('stops dwell while paused and resumes on play', () => {
    startWatching();
    advance(3000);
    pause(v1());
    advance(5000);
    play(v1());
    advance(2000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(5000);
  });

  it('a pause that lasts to the end of the viewing adds nothing after it', () => {
    startWatching();
    advance(1500);
    pause(v1());
    advance(60_000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(1500);
  });

  it('buffering (not enough data) and the end of the video stop the clock', () => {
    startWatching();
    advance(1000);
    setVideo(v1(), { readyState: 2 });
    v1().dispatchEvent(new Event('waiting'));
    advance(4000);
    setVideo(v1(), { readyState: 4 });
    v1().dispatchEvent(new Event('canplay'));
    advance(1000);
    setVideo(v1(), { ended: true });
    v1().dispatchEvent(new Event('ended'));
    advance(9000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(2000);
  });
});

describe('hidden tab', () => {
  it('stops dwell while the tab is hidden and resumes when it is visible again', () => {
    startWatching();
    advance(2000);
    setVisibility('hidden');
    advance(30_000);
    setVisibility('visible');
    advance(3000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(5000);
    expect(events).toHaveLength(1); // hiding the tab does not end or split the viewing
  });

  it('after the tab is visible again the clock stays off while the video is still paused', () => {
    startWatching();
    advance(1000);
    setVisibility('hidden');
    pause(v1());
    advance(1000);
    setVisibility('visible');
    advance(5000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(1000);
  });

  it('a tab that starts hidden accumulates nothing', () => {
    setVisibility('hidden');
    startWatching();
    advance(7000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(0);
  });

  it('an unknown visibility state does not count as visible', () => {
    startWatching();
    advance(1000);
    setVisibility('prerender');
    advance(5000);
    io().fire([v1(), 0]);
    expect(events[0]?.body.dwell_ms).toBe(1000);
  });
});

describe('switching Reel', () => {
  it('ends the first viewing and starts the second at the same moment', () => {
    startWatching();
    advance(3000);
    play(v2());
    io().fire([v1(), 0.3, 300], [v2(), 0.8, 800]);
    expect(events).toHaveLength(1);
    expect(events[0]?.body.reel_shortcode).toBe('Aaaaa11111_');
    expect(events[0]?.body.dwell_ms).toBe(3000);
    expect(tracker.isOpen()).toBe(true);

    advance(2000);
    io().fire([v2(), 0.1]);
    expect(events).toHaveLength(2);
    expect(events[1]?.body.reel_shortcode).toBe('Bbbbb22222_');
    expect(events[1]?.body.dwell_ms).toBe(2000);
    expect(events[1]?.body.creator_handle).toEqual({ value: 'second_creator', status: 'available' });
    expect(events[1]?.event_id).not.toBe(events[0]?.event_id);
  });

  it('time as primary of the other Reel is not added to the first Reel', () => {
    startWatching();
    advance(1000);
    play(v2());
    io().fire([v1(), 0.2], [v2(), 0.9]);
    advance(9000);
    io().fire([v2(), 0]);
    expect(events.map((e) => e.body.dwell_ms)).toEqual([1000, 9000]);
  });

  it('events from the old primary after the switch change nothing', () => {
    startWatching();
    advance(1000);
    play(v2());
    io().fire([v1(), 0.2], [v2(), 0.9]);
    advance(500);
    pause(v1()); // v1 is no longer primary
    advance(500);
    io().fire([v2(), 0]);
    expect(events.map((e) => e.body.dwell_ms)).toEqual([1000, 1000]);
  });
});

describe('repeated Reel', () => {
  it('scrolling away and back is a second viewing with its own event_id', () => {
    startWatching();
    advance(2000);
    play(v2());
    io().fire([v1(), 0.2], [v2(), 0.9]);
    advance(1000);
    io().fire([v2(), 0.2], [v1(), 0.9]);
    advance(3000);
    io().fire([v1(), 0]);

    expect(events.map((e) => e.body.reel_shortcode)).toEqual([
      'Aaaaa11111_',
      'Bbbbb22222_',
      'Aaaaa11111_',
    ]);
    expect(events.map((e) => e.body.dwell_ms)).toEqual([2000, 1000, 3000]);
    expect(new Set(events.map((e) => e.event_id)).size).toBe(3);
  });
});

describe('pagehide', () => {
  it('ends the open viewing and emits it once', () => {
    startWatching();
    advance(4000);
    tracker.pageHide();
    expect(events).toHaveLength(1);
    expect(events[0]?.body.dwell_ms).toBe(4000);
    expect(tracker.isOpen()).toBe(false);
  });

  it('is safe to repeat, and nothing is emitted by stop or a later observer callback', () => {
    startWatching();
    advance(1000);
    const watching = io();
    tracker.pageHide();
    tracker.pageHide();
    tracker.stop();
    watching.fire([v1(), 0]);
    expect(events).toHaveLength(1);
    expect(watching.disconnected).toBe(true);
  });

  it('with no open viewing it emits nothing and does not throw', () => {
    tracker.start();
    expect(() => tracker.pageHide()).not.toThrow();
    expect(events).toEqual([]);
  });

  it('keeps what it read at the start when the Reel was already removed from the page', () => {
    startWatching();
    advance(2500);
    v1().closest('section')?.remove(); // virtualised list unmounts the Reel
    tracker.pageHide();
    expect(events[0]?.body).toMatchObject({
      reel_shortcode: 'Aaaaa11111_',
      dwell_ms: 2500,
      creator_handle: { value: 'first_creator', status: 'available' },
    });
  });

  it('a hidden tab at pagehide still ends the viewing, with dwell up to the hide', () => {
    startWatching();
    advance(2000);
    setVisibility('hidden');
    advance(10_000);
    tracker.pageHide();
    expect(events).toHaveLength(1);
    expect(events[0]?.body.dwell_ms).toBe(2000);
  });

  it('after a restore from the back/forward cache, a new viewing gets a new event_id', () => {
    startWatching();
    advance(1000);
    tracker.pageHide();
    const first = events[0]?.event_id;
    tracker.resume();
    io().fire([v1(), 1]);
    advance(2000);
    tracker.pageHide();
    expect(events).toHaveLength(2);
    expect(events[1]?.event_id).not.toBe(first);
    expect(events[1]?.body.dwell_ms).toBe(2000);
  });

  it('a failing send is counted, throws nothing, and is not retried here', () => {
    startWatching();
    advance(1000);
    emitThrows = true;
    expect(() => tracker.pageHide()).not.toThrow();
    expect(tracker.stats()).toEqual({ emitted: 0, droppedNoShortcode: 0, emitFailures: 1 });
    emitThrows = false;
    tracker.pageHide();
    expect(events).toEqual([]);
  });
});

describe('duplicate prevention', () => {
  it('repeated observer callbacks for the same primary make one viewing', () => {
    tracker.start();
    play(v1());
    io().fire([v1(), 0.7]);
    io().fire([v1(), 0.9]);
    io().fire([v1(), 1]);
    advance(1000);
    io().fire([v1(), 0]);
    io().fire([v1(), 0]);
    expect(events).toHaveLength(1);
  });

  it('leaving the Reel and then pagehide emits one event, not two', () => {
    startWatching();
    advance(1000);
    io().fire([v1(), 0]);
    tracker.pageHide();
    expect(events).toHaveLength(1);
  });

  it('pagehide then stop emits one event', () => {
    startWatching();
    tracker.pageHide();
    tracker.stop();
    expect(events).toHaveLength(1);
  });

  it('a metadata refresh after the viewing ended emits nothing', () => {
    startWatching();
    io().fire([v1(), 0]);
    runRefresh();
    expect(events).toHaveLength(1);
  });

  it('the service worker keeps one queued row when the same event arrives twice', async () => {
    const sw = makeEnv();
    await sw.ready();
    await startSession(sw.deps);
    startWatching();
    advance(1000);
    io().fire([v1(), 0]);
    const e = events[0];
    const message = { type: 'reelEvent', event_id: e?.event_id, body: e?.body };
    const sender = { id: 'ext', tabId: 7, tabUrl: REELS_URL };
    expect(await routeMessage(sw.deps, 'ext', message, sender)).toEqual({ accepted: true });
    expect(await routeMessage(sw.deps, 'ext', message, sender)).toEqual({ accepted: true });
    const db = await sw.deps.openQueue();
    expect(await countEvents(db)).toBe(1);
    db.close();
  });
});

describe('no value is invented', () => {
  it('a Reel with no readable caption, handle or duration reports each as unavailable', () => {
    load(fixture('reel-broken.html'));
    href = 'https://www.instagram.com/reels/Zurl44444_q/';
    tracker.start();
    play(v1());
    io().fire([v1(), 1]);
    advance(1000);
    io().fire([v1(), 0]);
    expect(events[0]?.body).toMatchObject({
      reel_shortcode: 'Zurl44444_q',
      video_duration_ms: null,
      video_duration_status: 'unavailable',
      creator_handle: { value: null, status: 'unavailable' },
      caption: { value: null, status: 'unavailable' },
      hashtags: { value: null, status: 'unavailable' },
    });
    expect(isExposureBody(events[0]?.body)).toBe(true);
  });

  it('drops a viewing that cannot be identified, and counts it', () => {
    load(fixture('reel-broken.html'));
    href = 'https://www.instagram.com/reels/'; // no code in the address
    tracker.start();
    play(v1());
    io().fire([v1(), 1]);
    io().fire([v1(), 0]);
    expect(events).toEqual([]);
    expect(tracker.stats().droppedNoShortcode).toBe(1);
  });

  it('flags a truncated caption instead of presenting it as complete', () => {
    load(fixture('reel-truncated.html'));
    tracker.start();
    play(v1());
    io().fire([v1(), 1]);
    io().fire([v1(), 0]);
    expect(events[0]?.body.caption_truncated).toBe(true);
  });
});

describe('metadata that loads late', () => {
  it(`is read again ${METADATA_REFRESH_MS} ms after the Reel becomes primary`, () => {
    load(fixture('reel-no-caption.html'));
    tracker.start();
    play(v1());
    io().fire([v1(), 1]);
    expect(scheduled.map((s) => s.ms)).toEqual([METADATA_REFRESH_MS]);

    const span = document.createElement('span');
    span.setAttribute('dir', 'auto');
    span.textContent = 'Late caption #late';
    v1().parentElement?.parentElement?.append(span);
    runRefresh();
    io().fire([v1(), 0]);

    expect(events[0]?.body.caption).toEqual({ value: 'Late caption #late', status: 'available' });
    expect(events[0]?.body.hashtags).toEqual({ value: ['late'], status: 'available' });
  });

  it('a later unavailable read does not erase a value that was read earlier', () => {
    startWatching();
    v1().closest('section')?.querySelector('span')?.remove();
    runRefresh();
    io().fire([v1(), 0]);
    expect(events[0]?.body.caption.status).toBe('available');
  });

  it('a link in the Reel markup replaces a shortcode taken from a lagging page address', () => {
    load(fixture('reel-no-caption.html'));
    document.querySelectorAll('a[href^="/reel/"]').forEach((a) => a.remove());
    href = 'https://www.instagram.com/reels/Oldxx99999_/'; // still shows the previous Reel
    tracker.start();
    play(v1());
    io().fire([v1(), 1]);

    const link = document.createElement('a');
    link.setAttribute('href', '/reel/Newxx88888_/');
    v1().parentElement?.parentElement?.append(link);
    runRefresh();
    io().fire([v1(), 0]);
    expect(events[0]?.body.reel_shortcode).toBe('Newxx88888_');
  });

  it('does not refresh after the viewing is over: the timer is cancelled', () => {
    startWatching();
    io().fire([v1(), 0]);
    expect(scheduled.every((s) => s.cancelled)).toBe(true);
  });
});

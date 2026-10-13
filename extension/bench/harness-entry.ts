/**
 * Browser benchmark harness (N1, N2). Bundled by `browser-bench.mjs` and run inside Chromium on a
 * generated feed page. It drives the REAL content-script code (ig-adapter.ts and the exposure
 * tracker) with the browser's own IntersectionObserver and MutationObserver; only the page is
 * synthetic. See docs/performance-report.md for what that does and does not show.
 */

import { readReel } from '../src/ig-adapter';
import { createExposureTracker, type ExposureTracker } from '../src/exposure/tracker';

interface Timings {
  io: number[];
  mo: number[];
  detections: number[];
  emitted: number;
}

declare global {
  interface Window {
    __bench: Harness;
  }
}

const REEL_URL = 'https://www.instagram.com/reels/Aaaaa11111_/';

function makeSection(index: number, filler: number): HTMLElement {
  const section = document.createElement('section');
  section.dataset['i'] = String(index);
  let host: HTMLElement = section;
  for (let depth = 0; depth < 12; depth += 1) {
    const wrapper = document.createElement('div');
    host.appendChild(wrapper);
    host = wrapper;
  }
  const video = document.createElement('video');
  video.muted = true;
  host.appendChild(video);
  const code = `R${String(index).padStart(5, '0')}abcde`;
  const creator = document.createElement('a');
  creator.href = `/creator_${index % 50}/`;
  creator.textContent = `creator_${index % 50}`;
  const link = document.createElement('a');
  link.href = `/reel/${code}/`;
  link.textContent = 'link';
  const caption = document.createElement('span');
  caption.setAttribute('dir', 'auto');
  caption.textContent = `Caption number ${index} about cricket and food #cricket #food`;
  section.append(creator, link, caption);
  // The real page is far heavier than a fixture: buttons, counts, avatars, overlays.
  for (let i = 0; i < filler; i += 1) {
    const row = document.createElement('div');
    row.innerHTML =
      '<button type="button"><span>12K</span></button><a href="/x/"><img alt="p" width="1" height="1"></a>';
    section.appendChild(row);
  }
  return section;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export class Harness {
  feed: HTMLElement;
  tracker: ExposureTracker | null = null;
  timings: Timings = { io: [], mo: [], detections: [], emitted: 0 };
  private next = 0;
  private filler = 150;

  constructor() {
    document.head.insertAdjacentHTML(
      'beforeend',
      '<style>html,body{margin:0;height:100%}#feed{height:100vh;overflow-y:scroll;position:relative}' +
        'section{height:100vh;position:relative}video{width:100%;height:60%;background:#222}</style>',
    );
    document.body.innerHTML = '<div id="feed"></div>';
    this.feed = document.getElementById('feed') as HTMLElement;
  }

  build(reels: number, filler: number): number {
    this.filler = filler;
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < reels; i += 1) fragment.appendChild(makeSection(this.next++, filler));
    this.feed.appendChild(fragment);
    return document.querySelectorAll('*').length;
  }

  startTracker(): void {
    this.timings = { io: [], mo: [], detections: [], emitted: 0 };
    const timings = this.timings;
    const TimedIO = class extends IntersectionObserver {
      constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
        super((entries, observer) => {
          const t0 = performance.now();
          callback(entries, observer);
          timings.io.push(performance.now() - t0);
        }, options);
      }
    };
    const TimedMO = class extends MutationObserver {
      constructor(callback: MutationCallback) {
        super((records, observer) => {
          const t0 = performance.now();
          callback(records, observer);
          timings.mo.push(performance.now() - t0);
        });
      }
    };
    this.tracker = createExposureTracker({
      env: {
        doc: document,
        href: () => REEL_URL,
        IntersectionObserverCtor: TimedIO,
        MutationObserverCtor: TimedMO,
      },
      now: () => performance.now(),
      wallNow: () => Date.now(),
      // begin() calls newId() when a Reel becomes primary: that moment is "detected".
      newId: () => {
        timings.detections.push(performance.now());
        return crypto.randomUUID();
      },
      emit: () => {
        timings.emitted += 1;
      },
      schedule: (fn, ms) => {
        const id = window.setTimeout(fn, ms);
        return () => window.clearTimeout(id);
      },
    });
    this.tracker.start();
  }

  stopTracker(): void {
    this.tracker?.stop();
    this.tracker = null;
  }

  snapshot(): Timings {
    return JSON.parse(JSON.stringify(this.timings)) as Timings;
  }

  /**
   * Scroll to each Reel in turn. Two latencies per step, both until the tracker has started the
   * viewing instance: `fromReference`, measured from the browser's own timestamp of the Reel
   * reaching 60% visible (a second IntersectionObserver, `entry.time`, the same clock as
   * performance.now), and `fromCommand`, measured from the scroll call.
   */
  async detection(
    steps: number,
    mode: 'instant' | 'smooth',
  ): Promise<{ fromReference: number[]; fromCommand: number[]; missed: number }> {
    const vh = window.innerHeight;
    const fromReference: number[] = [];
    const fromCommand: number[] = [];
    let missed = 0;
    for (let i = 1; i <= steps; i += 1) {
      const section = this.feed.children[i] as HTMLElement | undefined;
      if (section === undefined) break;
      const before = this.timings.detections.length;
      let crossedAt: number | null = null;
      const reference = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (crossedAt === null && e.intersectionRatio >= 0.6) crossedAt = e.time;
          }
        },
        { threshold: [0.6] },
      );
      reference.observe(section.querySelector('video') as Element);
      const commanded = performance.now();
      this.feed.scrollTo({ top: i * vh, behavior: mode });
      const end = performance.now() + 2500;
      while (this.timings.detections.length === before && performance.now() < end) await sleep(2);
      await sleep(30);
      reference.disconnect();
      const detected = this.timings.detections[before];
      if (this.timings.detections.length === before || detected === undefined || crossedAt === null) {
        missed += 1;
      } else {
        fromReference.push(detected - crossedAt);
        fromCommand.push(detected - commanded);
      }
      await this.settle();
    }
    return { fromReference, fromCommand, missed };
  }

  /** Wait until the feed has stopped scrolling, so one step never overlaps the next. */
  private async settle(): Promise<void> {
    let last = this.feed.scrollTop;
    let still = 0;
    const end = performance.now() + 2500;
    while (still < 3 && performance.now() < end) {
      await sleep(40);
      still = this.feed.scrollTop === last ? still + 1 : 0;
      last = this.feed.scrollTop;
    }
  }

  /** Time `readReel` alone (the metadata read the tracker does when a Reel becomes primary). */
  readCost(rounds: number): number[] {
    const times: number[] = [];
    const videos = [...document.getElementsByTagName('video')];
    for (let r = 0; r < rounds; r += 1) {
      const video = videos[r % videos.length] as HTMLVideoElement;
      const t0 = performance.now();
      readReel(
        { doc: document, href: () => REEL_URL, IntersectionObserverCtor: IntersectionObserver, MutationObserverCtor: MutationObserver },
        video,
      );
      times.push(performance.now() - t0);
    }
    return times;
  }

  /** Append `k` Reels at the bottom and drop `k` from the top, `rounds` times (a virtualised feed). */
  async churn(k: number, rounds: number): Promise<void> {
    for (let r = 0; r < rounds; r += 1) {
      const fragment = document.createDocumentFragment();
      for (let i = 0; i < k; i += 1) fragment.appendChild(makeSection(this.next++, this.filler));
      this.feed.appendChild(fragment);
      for (let i = 0; i < k && this.feed.children.length > 12; i += 1) {
        this.feed.removeChild(this.feed.children[0] as Element);
      }
      await sleep(30);
    }
  }

  /** Frame times while the feed is flicked from Reel to Reel for `seconds`. */
  async scrollFrames(seconds: number): Promise<{ frames: number[]; longTasks: number[] }> {
    const frames: number[] = [];
    const longTasks: number[] = [];
    const observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) longTasks.push(entry.duration);
    });
    observer.observe({ type: 'longtask', buffered: false });
    const vh = window.innerHeight;
    const total = this.feed.children.length;
    let last = performance.now();
    const end = last + seconds * 1000;
    let nextFlick = last;
    let index = Math.round(this.feed.scrollTop / vh);
    await new Promise<void>((resolve) => {
      const tick = (now: number) => {
        frames.push(now - last);
        last = now;
        if (now >= nextFlick) {
          index = (index + 1) % Math.max(2, total - 1);
          this.feed.scrollTo({ top: index * vh, behavior: 'smooth' });
          nextFlick = now + 700;
        }
        if (now < end) requestAnimationFrame(tick);
        else resolve();
      };
      requestAnimationFrame(tick);
    });
    observer.disconnect();
    return { frames: frames.slice(2), longTasks };
  }
}

window.__bench = new Harness();

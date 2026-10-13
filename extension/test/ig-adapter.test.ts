import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ADAPTER_VERSION,
  PRIMARY_MIN_RATIO,
  createPrimaryTracker,
  readPlayback,
  readReel,
  readShortcodeFromLinks,
  readShortcodeFromUrl,
  runSelfTest,
  type AdapterEnv,
} from '../src/ig-adapter';

// ---------- helpers ----------

const REELS = 'https://www.instagram.com/reels/';

function fixture(name: string): string {
  return readFileSync(join(__dirname, 'fixtures', name), 'utf8');
}

function load(html: string): void {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  document.body.replaceChildren(...Array.from(parsed.body.childNodes));
}

function video(id = 'v1'): HTMLVideoElement {
  const el = document.getElementById(id);
  if (!(el instanceof HTMLVideoElement)) throw new Error(`fixture has no video #${id}`);
  return el;
}

function setVideo(
  el: HTMLVideoElement,
  state: Partial<{ duration: number; paused: boolean; ended: boolean; readyState: number }>,
): void {
  for (const [key, value] of Object.entries(state)) {
    Object.defineProperty(el, key, { value, configurable: true });
  }
}

class FakeIO {
  static last: FakeIO | null = null;
  observed = new Set<Element>();
  disconnected = false;
  constructor(
    readonly callback: IntersectionObserverCallback,
    readonly options?: IntersectionObserverInit,
  ) {
    FakeIO.last = this;
  }
  observe(el: Element): void {
    this.observed.add(el);
  }
  unobserve(el: Element): void {
    this.observed.delete(el);
  }
  disconnect(): void {
    this.disconnected = true;
    this.observed.clear();
  }
  fire(...items: [Element, number, number?][]): void {
    const entries = items.map(([target, ratio, area = 1000]) => ({
      target,
      intersectionRatio: ratio,
      intersectionRect: { width: area, height: 1 },
    }));
    this.callback(entries as unknown as IntersectionObserverEntry[], this as never);
  }
}

class FakeMO {
  static last: FakeMO | null = null;
  disconnected = false;
  constructor(readonly callback: MutationCallback) {
    FakeMO.last = this;
  }
  observe(): void {}
  disconnect(): void {
    this.disconnected = true;
  }
  fire(added: Node[], removed: Node[] = []): void {
    this.callback(
      [{ addedNodes: added, removedNodes: removed } as unknown as MutationRecord],
      this as never,
    );
  }
}

let href = REELS;
const env = (over: Partial<AdapterEnv> = {}): AdapterEnv => ({
  doc: document,
  href: () => href,
  IntersectionObserverCtor: FakeIO as never,
  MutationObserverCtor: FakeMO as never,
  ...over,
});

beforeEach(() => {
  href = REELS;
  FakeIO.last = null;
  FakeMO.last = null;
  document.body.replaceChildren();
});

// ---------- field extraction ----------

describe('readReel on a full reel', () => {
  beforeEach(() => {
    load(fixture('reel-full.html'));
    setVideo(video(), { duration: 15.04 });
  });

  it('reads every field with its status and source', () => {
    const m = readReel(env(), video());
    expect(m.adapter_version).toBe(ADAPTER_VERSION);
    expect(m.shortcode).toEqual({ status: 'available', value: 'Cabc12345_x', source: 'dom_link' });
    expect(m.creator_handle).toEqual({ status: 'available', value: 'creator_one', source: 'profile_link' });
    expect(m.caption).toMatchObject({ status: 'available', source: 'dir_auto' });
    expect(m.caption.status === 'available' && m.caption.value).toContain('Morning run, 5 km');
    expect(m.caption_truncated).toBe(false);
    expect(m.hashtags).toEqual({ status: 'available', value: ['running', 'fitness'], source: 'tag_links' });
    expect(m.video_duration_ms).toEqual({ status: 'available', value: 15040, source: 'video_element' });
  });

  it('does not take the handle from page navigation outside the reel', () => {
    const m = readReel(env(), video());
    expect(m.creator_handle.status === 'available' && m.creator_handle.value).not.toBe('nav_owner');
  });

  it('does not depend on class names or ids other than the video', () => {
    const before = readReel(env(), video());
    for (const el of document.querySelectorAll('[class]')) el.removeAttribute('class');
    expect(readReel(env(), video())).toEqual(before);
  });
});

describe('field status: nothing is defaulted', () => {
  it('no caption: caption unavailable, hashtags unavailable, the rest still read', () => {
    load(fixture('reel-no-caption.html'));
    const m = readReel(env(), video());
    expect(m.caption).toEqual({ status: 'unavailable', reason: 'no_caption_text' });
    expect(m.hashtags).toEqual({ status: 'unavailable', reason: 'no_tag_links_and_no_caption' });
    expect(m.shortcode).toMatchObject({ status: 'available', value: 'Dxyz98765_y' });
    expect(m.creator_handle).toMatchObject({ status: 'available', value: 'creator_two' });
    expect(m.video_duration_ms).toEqual({ status: 'unavailable', reason: 'duration_not_loaded' });
  });

  it('a readable caption without hashtags gives an empty list, not unavailable', () => {
    load(fixture('feed-two-reels.html'));
    document.getElementById('s1')?.querySelector('span')?.replaceChildren('No tags here');
    expect(readReel(env(), video('v1')).hashtags).toEqual({
      status: 'available',
      value: [],
      source: 'caption_text',
    });
  });

  it('a page that changed shape reports every page field unavailable', () => {
    load(fixture('reel-broken.html'));
    const m = readReel(env({ href: () => 'https://www.instagram.com/' }), video());
    expect(m.shortcode.status).toBe('unavailable');
    expect(m.creator_handle.status).toBe('unavailable');
    expect(m.caption.status).toBe('unavailable');
    expect(m.hashtags.status).toBe('unavailable');
    expect(m.video_duration_ms.status).toBe('unavailable');
  });

  it('marks a truncated caption and never clicks the more control', () => {
    load(fixture('reel-truncated.html'));
    const click = vi.fn();
    document.body.addEventListener('click', click);
    const m = readReel(env(), video());
    expect(m.caption).toMatchObject({ status: 'available', source: 'h1' });
    expect(m.caption_truncated).toBe(true);
    expect(m.hashtags).toMatchObject({ status: 'available', value: ['travel'] });
    expect(click).not.toHaveBeenCalled();
  });

  it('reads Hindi captions and hashtags', () => {
    load(fixture('reel-hindi.html'));
    const m = readReel(env(), video());
    expect(m.hashtags).toMatchObject({ status: 'available', value: ['खाना', 'recipe'] });
    expect(m.shortcode).toMatchObject({ value: 'Fhin33333_h' });
    expect(m.creator_handle).toMatchObject({ value: 'creator.four_' });
  });

  it.each([
    ['not loaded', Number.NaN, 'duration_not_loaded'],
    ['live or unbounded', Number.POSITIVE_INFINITY, 'duration_unbounded'],
    ['zero', 0, 'duration_zero'],
  ])('video duration %s is unavailable', (_name, duration, reason) => {
    load(fixture('reel-full.html'));
    setVideo(video(), { duration });
    expect(readReel(env(), video()).video_duration_ms).toEqual({ status: 'unavailable', reason });
  });
});

describe('two reels in the DOM: each video reads its own metadata', () => {
  beforeEach(() => load(fixture('feed-two-reels.html')));

  it('reads reel 1 and reel 2 separately', () => {
    const one = readReel(env(), video('v1'));
    const two = readReel(env(), video('v2'));
    expect(one.shortcode).toMatchObject({ value: 'Aaaaa11111_' });
    expect(one.creator_handle).toMatchObject({ value: 'first_creator' });
    expect(one.hashtags).toMatchObject({ value: ['one'] });
    expect(two.shortcode).toMatchObject({ value: 'Bbbbb22222_' });
    expect(two.creator_handle).toMatchObject({ value: 'second_creator' });
    expect(two.hashtags).toMatchObject({ value: ['two', 'deux'] });
  });

  it('prefers the reel link over the page address, which may lag', () => {
    href = 'https://www.instagram.com/reels/Aaaaa11111_/';
    expect(readReel(env(), video('v2')).shortcode).toMatchObject({
      value: 'Bbbbb22222_',
      source: 'dom_link',
    });
  });
});

describe('reading a reel in a long feed stays cheap (docs/performance-report.md)', () => {
  /** A feed of `n` reels, each with a video, links, a caption and `filler` unrelated elements. */
  function longFeed(n: number, filler: number): void {
    const feed = document.createElement('div');
    for (let i = 0; i < n; i += 1) {
      const section = document.createElement('section');
      const wrap = document.createElement('div');
      const v = document.createElement('video');
      v.id = `feed-v${i}`;
      wrap.appendChild(v);
      section.append(wrap);
      section.insertAdjacentHTML(
        'beforeend',
        `<a href="/creator_${i}/">c</a><a href="/reel/Code${String(i).padStart(5, '0')}x/">l</a>` +
          `<span dir="auto">Caption ${i} #tag${i}</span>` +
          '<div><span>1</span></div>'.repeat(filler),
      );
      feed.appendChild(section);
    }
    document.body.replaceChildren(feed);
  }

  it('never counts every video below an ancestor: it asks for the second one only', () => {
    longFeed(40, 30);
    const spy = vi.spyOn(Element.prototype, 'querySelectorAll');
    const read = readReel(env(), video('feed-v7'));
    const countedVideos = spy.mock.calls.filter(([selector]) => selector === 'video');
    spy.mockRestore();
    expect(countedVideos).toHaveLength(0);
    expect(read.shortcode).toMatchObject({ value: 'Code00007x', source: 'dom_link' });
  });

  it('still stops climbing at the level that holds a second video', () => {
    longFeed(5, 3);
    const read = readReel(env(), video('feed-v2'));
    // Only this reel's own links and caption are read, never a neighbour's.
    expect(read.shortcode).toMatchObject({ value: 'Code00002x' });
    expect(read.creator_handle).toMatchObject({ value: 'creator_2' });
    expect(read.caption).toMatchObject({ value: 'Caption 2 #tag2' });
    expect(read.hashtags).toMatchObject({ value: ['tag2'] });
  });
});

describe('shortcode', () => {
  it('falls back to the page address when the DOM has no link', () => {
    load(fixture('reel-broken.html'));
    href = 'https://www.instagram.com/reels/Zurl44444_q/';
    expect(readReel(env(), video()).shortcode).toEqual({
      status: 'available',
      value: 'Zurl44444_q',
      source: 'url',
    });
  });

  it('refuses to guess between several different reel links', () => {
    load(fixture('reel-ambiguous.html'));
    href = 'https://www.instagram.com/reels/Zurl44444_q/';
    const m = readReel(env(), video());
    // The ambiguous links are not used; the address is the only remaining source.
    expect(m.shortcode).toMatchObject({ source: 'url', value: 'Zurl44444_q' });
    href = REELS;
    expect(readReel(env(), video()).shortcode).toEqual({ status: 'unavailable', reason: 'no_code_in_url' });
  });

  it('reports ambiguous links as such, and ignores the audio page link', () => {
    load(fixture('reel-ambiguous.html'));
    expect(readShortcodeFromLinks(video(), document)).toEqual({
      status: 'unavailable',
      reason: 'ambiguous_links',
    });
  });

  it('ignores links that point away from instagram.com', () => {
    load(fixture('reel-broken.html'));
    const link = document.createElement('a');
    link.setAttribute('href', 'https://evil.example/reel/Xxxxx11111_/');
    video().parentElement?.append(link);
    expect(readShortcodeFromLinks(video(), document)).toEqual({
      status: 'unavailable',
      reason: 'no_link',
    });
  });

  it('ignores the audio page link and reserved words', () => {
    expect(readShortcodeFromUrl('https://www.instagram.com/reels/audio/')).toEqual({
      status: 'unavailable',
      reason: 'no_code_in_url',
    });
  });

  it.each([
    ['https://www.instagram.com/reels/Cabc12345_x/', 'Cabc12345_x'],
    ['https://www.instagram.com/reels/Cabc12345_x', 'Cabc12345_x'],
    ['https://www.instagram.com/reel/Cabc12345_x/', 'Cabc12345_x'],
    ['https://www.instagram.com/reels/Cabc12345_x/?igsh=abc', 'Cabc12345_x'],
  ])('reads the code from %s', (url, code) => {
    expect(readShortcodeFromUrl(url)).toMatchObject({ status: 'available', value: code });
  });

  it.each([
    'https://www.instagram.com/reels/',
    'https://www.instagram.com/',
    'https://www.instagram.com/someone/',
    'https://www.instagram.com/p/Cabc12345_x/',
    'https://example.com/reels/Cabc12345_x/',
    'not a url',
  ])('finds no code in %s', (url) => {
    expect(readShortcodeFromUrl(url).status).toBe('unavailable');
  });
});

// ---------- playing state and tab visibility ----------

describe('readPlayback', () => {
  beforeEach(() => load(fixture('reel-full.html')));

  const visibility = (state: string) =>
    Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });

  it('is playing only when not paused, not ended and data is buffered', () => {
    visibility('visible');
    const v = video();
    setVideo(v, { paused: false, ended: false, readyState: 4 });
    expect(readPlayback(env(), v).playing).toEqual({ status: 'available', value: true, source: 'video_element' });
    setVideo(v, { paused: true });
    expect(readPlayback(env(), v).playing).toMatchObject({ value: false });
    setVideo(v, { paused: false, ended: true });
    expect(readPlayback(env(), v).playing).toMatchObject({ value: false });
    setVideo(v, { ended: false, readyState: 2 });
    expect(readPlayback(env(), v).playing).toMatchObject({ value: false });
  });

  it('playing is unavailable for a video that left the page', () => {
    const v = video();
    v.remove();
    expect(readPlayback(env(), v).playing).toEqual({ status: 'unavailable', reason: 'video_detached' });
  });

  it('reads tab visibility, and leaves unknown states unavailable', () => {
    const v = video();
    visibility('visible');
    expect(readPlayback(env(), v).tab_visible).toMatchObject({ status: 'available', value: true });
    visibility('hidden');
    expect(readPlayback(env(), v).tab_visible).toMatchObject({ status: 'available', value: false });
    visibility('prerender');
    expect(readPlayback(env(), v).tab_visible).toEqual({
      status: 'unavailable',
      reason: 'unknown_visibility_state',
    });
    visibility('visible');
  });
});

// ---------- primary Reel ----------

describe('primary tracker', () => {
  let changes: (string | null)[];
  const onChange = (v: HTMLVideoElement | null) => changes.push(v?.id ?? null);
  const start = () => {
    const t = createPrimaryTracker(env(), onChange);
    t.start();
    return t;
  };

  beforeEach(() => {
    changes = [];
    load(fixture('feed-two-reels.html'));
  });

  it(`uses an IntersectionObserver with a threshold at ${PRIMARY_MIN_RATIO}`, () => {
    start();
    expect(FakeIO.last?.options?.threshold).toContain(PRIMARY_MIN_RATIO);
    expect([...(FakeIO.last?.observed ?? [])].map((e) => e.id)).toEqual(['v1', 'v2']);
  });

  it('needs at least 60% visible: 59% is not primary, 60% is', () => {
    const t = start();
    FakeIO.last?.fire([video('v1'), 0.59]);
    expect(t.current()).toBeNull();
    expect(changes).toEqual([]);
    FakeIO.last?.fire([video('v1'), 0.6]);
    expect(t.current()?.id).toBe('v1');
    expect(changes).toEqual(['v1']);
  });

  it('reports a change only when the primary changes', () => {
    start();
    FakeIO.last?.fire([video('v1'), 0.7]);
    FakeIO.last?.fire([video('v1'), 0.9]);
    expect(changes).toEqual(['v1']);
  });

  it('hands over from one reel to the next and to nothing', () => {
    const t = start();
    FakeIO.last?.fire([video('v1'), 1, 900]);
    FakeIO.last?.fire([video('v1'), 0.3, 300], [video('v2'), 0.7, 700]);
    expect(t.current()?.id).toBe('v2');
    FakeIO.last?.fire([video('v2'), 0.2, 200]);
    expect(t.current()).toBeNull();
    expect(changes).toEqual(['v1', 'v2', null]);
  });

  it('with two qualifying videos the larger visible area wins; a tie keeps the current one', () => {
    const t = start();
    FakeIO.last?.fire([video('v1'), 0.8, 500], [video('v2'), 0.9, 800]);
    expect(t.current()?.id).toBe('v2');
    FakeIO.last?.fire([video('v1'), 0.8, 800]);
    expect(t.current()?.id).toBe('v2');
  });

  it('drops a primary that is removed from the page', () => {
    const t = start();
    FakeIO.last?.fire([video('v1'), 1]);
    const v1 = video('v1');
    v1.closest('section')?.remove();
    FakeMO.last?.fire([], [v1]);
    expect(t.current()).toBeNull();
    expect(changes).toEqual(['v1', null]);
    expect(FakeIO.last?.observed.has(v1)).toBe(false);
  });

  it('starts watching videos that are added later, including inside an added subtree', () => {
    start();
    const section = document.createElement('section');
    section.innerHTML = '<div><video id="v3"></video></div>';
    document.querySelector('main')?.append(section);
    FakeMO.last?.fire([section, document.createTextNode('x')]);
    expect([...(FakeIO.last?.observed ?? [])].map((e) => e.id)).toContain('v3');
    FakeIO.last?.fire([video('v3'), 1]);
    expect(changes).toEqual(['v3']);
  });

  it('ignores observer entries for elements it does not track', () => {
    const t = start();
    FakeIO.last?.fire([document.body, 1]);
    expect(t.current()).toBeNull();
  });

  it('stop disconnects both observers and clears the primary', () => {
    const t = start();
    const io = FakeIO.last;
    FakeIO.last?.fire([video('v1'), 1]);
    t.stop();
    expect(io?.disconnected).toBe(true);
    expect(FakeMO.last?.disconnected).toBe(true);
    expect(t.current()).toBeNull();
    expect(changes).toEqual(['v1', null]);
  });

  it('throws when the browser has no IntersectionObserver', () => {
    expect(() => createPrimaryTracker(env({ IntersectionObserverCtor: undefined }), onChange)).toThrow();
  });
});

// ---------- self-test ----------

describe('self-test', () => {
  it('passes on a page that matches every assumption', () => {
    load(fixture('feed-two-reels.html'));
    setVideo(video('v1'), { duration: 12 });
    href = 'https://www.instagram.com/reels/Aaaaa11111_/';
    const report = runSelfTest(env(), video('v1'));
    expect(report.ok).toBe(true);
    expect(report.failing).toEqual([]);
    const byId = Object.fromEntries(report.checks.map((c) => [c.id, c]));
    expect(byId['A2_container']?.status).toBe('pass');
    expect(byId['A3_shortcode_agree']?.status).toBe('pass');
    expect(byId['A3_shortcode_link']?.detail).toBe('via dom_link');
    expect(byId['A4_creator_handle']?.detail).toBe('via profile_link');
    expect(byId['A5_caption']?.detail).toBe('via dir_auto');
    expect(byId['A6_hashtags']?.detail).toBe('via caption_text');
    expect(byId['video_duration']?.status).toBe('pass');
  });

  it('names the assumptions that fail when Instagram changes its structure', () => {
    load(fixture('reel-broken.html'));
    href = 'https://www.instagram.com/reels/';
    const report = runSelfTest(env(), video());
    expect(report.ok).toBe(false);
    expect(report.failing).toEqual(
      expect.arrayContaining(['A3_shortcode_link', 'A4_creator_handle']),
    );
    const byId = Object.fromEntries(report.checks.map((c) => [c.id, c]));
    expect(byId['A5_caption']?.status).toBe('warn');
    expect(byId['A6_hashtags']?.status).toBe('warn');
  });

  it('fails when the link and the address name different reels', () => {
    load(fixture('feed-two-reels.html'));
    href = 'https://www.instagram.com/reels/Bbbbb22222_/';
    const report = runSelfTest(env(), video('v1'));
    expect(report.failing).toContain('A3_shortcode_agree');
  });

  it('warns when the container is not bounded by another video', () => {
    load(fixture('reel-full.html'));
    const byId = Object.fromEntries(runSelfTest(env(), video()).checks.map((c) => [c.id, c]));
    expect(byId['A2_container']?.status).toBe('warn');
  });

  it('skips the checks that need a video when there is no primary, and does not fail them', () => {
    load(fixture('feed-two-reels.html'));
    const report = runSelfTest(env(), null);
    const skipped = report.checks.filter((c) => c.status === 'skipped').map((c) => c.id);
    expect(skipped).toEqual(expect.arrayContaining(['primary_video', 'A4_creator_handle', 'A5_caption']));
    expect(report.ok).toBe(true);
  });

  it('fails when there are no video elements, off the surface, or without IntersectionObserver', () => {
    href = 'https://www.instagram.com/';
    const report = runSelfTest(env({ IntersectionObserverCtor: undefined }), null);
    expect(report.failing).toEqual(
      expect.arrayContaining(['surface', 'intersection_observer', 'A1_video_elements']),
    );
  });

  it('contains no page text: handles, captions and codes stay out of the report', () => {
    load(fixture('reel-full.html'));
    href = 'https://www.instagram.com/reels/Cabc12345_x/';
    const text = JSON.stringify(runSelfTest(env(), video()));
    for (const secret of ['creator_one', 'Morning run', 'Cabc12345_x', 'running']) {
      expect(text).not.toContain(secret);
    }
  });
});

// ---------- isolation ----------

describe('the adapter is the only file with Instagram selectors', () => {
  const srcDir = join(__dirname, '..', 'src');
  const sources: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (name.endsWith('.ts')) sources.push(path);
    }
  };
  walk(srcDir);
  const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const rel = (p: string) => relative(srcDir, p).replace(/\\/g, '/');

  it('no other source file queries the DOM by selector or tag', () => {
    const pattern = /querySelector(All)?\s*\(|getElementsBy(TagName|ClassName)|\.closest\s*\(|\.matches\s*\(/;
    const offenders = sources
      .filter((p) => rel(p) !== 'ig-adapter.ts' && !rel(p).startsWith('popup/'))
      .filter((p) => pattern.test(strip(readFileSync(p, 'utf8'))))
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it('no other source file knows Instagram URL structure beyond the surface check', () => {
    const pattern = /explore\/tags|\/reel\/|profile picture|dir="auto"/;
    const offenders = sources
      .filter((p) => rel(p) !== 'ig-adapter.ts')
      .filter((p) => pattern.test(strip(readFileSync(p, 'utf8'))))
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it('the adapter reads no cookies or storage, makes no request and drives no input', () => {
    const source = strip(readFileSync(join(srcDir, 'ig-adapter.ts'), 'utf8'));
    for (const forbidden of [
      /cookie/i,
      /localStorage|sessionStorage|indexedDB|chrome\./,
      /\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket/,
      /\.(click|scrollTo|scrollBy|scrollIntoView|dispatchEvent|play|pause|focus|submit)\s*\(/,
      /new (MouseEvent|KeyboardEvent|WheelEvent|TouchEvent|PointerEvent)/,
      /history\.(pushState|replaceState)|location\.(assign|replace)/,
    ]) {
      expect(source).not.toMatch(forbidden);
    }
  });
});

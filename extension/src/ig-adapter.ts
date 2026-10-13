/**
 * Instagram Web adapter: the ONLY file that knows what Instagram's page looks like (spec N8).
 * Every selector, URL pattern and DOM assumption lives here, and a source-scan test keeps it so.
 *
 * Boundary (CONTRIBUTING.md): reads the rendered DOM and <video> element state only. No cookies, no
 * credentials, no network, no private APIs. It never scrolls, clicks, plays, pauses or navigates;
 * the "more" caption control is detected but never clicked.
 *
 * Instagram's class names are obfuscated and change often, so none are used. The assumptions are
 * structural, and each is listed in the self-test so a failing one is reported, not guessed around:
 *
 *  A1  The Reels surface shows reels as <video> elements.
 *  A2  A reel's metadata lives in ancestors of its <video>, below the first ancestor that also
 *      contains another <video> (the "container"). Nearer ancestors win over farther ones.
 *  A3  The reel's own link is <a href="/reel/<code>/"> (or /reels/<code>/ or /p/<code>/); the
 *      page URL /reels/<code>/ names the reel in view.
 *  A4  The creator is a link to a bare profile path <a href="/<handle>/">.
 *  A5  The caption is an <h1>, or else the longest user text carrying dir="auto".
 *  A6  Hashtags are links to /explore/tags/<tag>/ and/or #tags written in the caption.
 *  A7  A truncated caption has a "more" control (never clicked).
 *
 * Field rule (spec F5, CONTRIBUTING.md): every field is `available` with a value, or `unavailable` with
 * a reason. Nothing is ever defaulted. A caption that is truncated is `available` with
 * `caption_truncated: true`; it is partial and says so.
 */

import { isReelsSurface } from './session/surface';

export const ADAPTER_VERSION = 'ig-web-2026.10.1';

/** Spec F4: a video is the primary Reel when at least this share of it is visible. */
export const PRIMARY_MIN_RATIO = 0.6;

export type Field<T> =
  | { status: 'available'; value: T; /** which assumption produced it */ source: string }
  | { status: 'unavailable'; reason: string };

export interface ReelMetadata {
  adapter_version: string;
  shortcode: Field<string>;
  creator_handle: Field<string>;
  caption: Field<string>;
  /** True when the page shows a "more" control: the caption above is cut short. */
  caption_truncated: boolean;
  hashtags: Field<string[]>;
  video_duration_ms: Field<number>;
}

export interface PlaybackState {
  /** Not paused, not ended, and enough data buffered to be showing frames. */
  playing: Field<boolean>;
  tab_visible: Field<boolean>;
}

export interface AdapterEnv {
  doc: Document;
  /** Current page address. Read on every call: Instagram navigates without reloading. */
  href(): string;
  /** Injected so tests can drive them; undefined means the browser lacks the API. */
  IntersectionObserverCtor: typeof IntersectionObserver | undefined;
  MutationObserverCtor: typeof MutationObserver | undefined;
}

// ---------- the assumptions: all selectors and patterns, in one place ----------

const ORIGIN = 'https://www.instagram.com';
const MAX_CLIMB = 30;
const CODE = '[A-Za-z0-9_-]{5,}';
const REEL_LINK = new RegExp(`^/(?:reel|reels|p)/(${CODE})/?$`);
const URL_CODE = new RegExp(`^/(?:reel|reels)/(${CODE})/?$`);
const TAG_LINK = /^\/explore\/tags\/([^/]+)\/?$/;
const PROFILE_LINK = /^\/([A-Za-z0-9._]{1,30})\/?$/;
const HASHTAG_IN_TEXT = /#([\p{L}\p{M}\p{N}_]+)/gu;
const MORE_LABELS = new Set(['more', '… more', '...more', '... more', 'और', '… और']);
/** First path segments that are Instagram pages, not people (and not reel codes). */
const RESERVED = new Set([
  'explore', 'reels', 'reel', 'p', 'stories', 'direct', 'accounts', 'about', 'legal',
  'developer', 'tv', 'web', 'emails', 'challenge', 'session', 'privacy', 'terms', 'api',
  'graphql', 'ar', 'directory', 'lite', 'topics', 'your_activity', 'notifications',
  'audio', 'locations', 'create', 'oauth',
]);

const SEL = {
  video: 'video',
  anchors: 'a[href]',
  h1: 'h1',
  userText: '[dir="auto"]',
  moreControls: '[role="button"], button, span',
  avatar: 'img[alt]',
} as const;
const AVATAR_ALT = /^([A-Za-z0-9._]{1,30})'s profile picture$/; // English UI only; last resort

// ---------- helpers ----------

const unavailable = (reason: string): { status: 'unavailable'; reason: string } => ({
  status: 'unavailable',
  reason,
});
const available = <T>(value: T, source: string): Field<T> => ({
  status: 'available',
  value,
  source,
});

const clean = (text: string | null): string => (text ?? '').replace(/\s+/g, ' ').trim();

function pathOf(anchor: Element): string | null {
  const raw = anchor.getAttribute('href');
  if (raw === null) return null;
  try {
    const url = new URL(raw, ORIGIN);
    if (url.origin !== ORIGIN) return null; // only Instagram's own links
    return url.pathname;
  } catch {
    return null;
  }
}

/** Ancestors of the video, nearest first, stopping before one that holds another video. */
export interface Climb {
  levels: Element[];
  /** True when the climb stopped because a sibling reel's video was reached. */
  bounded: boolean;
}

function climb(video: Element, doc: Document): Climb {
  const levels: Element[] = [];
  let el = video.parentElement;
  let bounded = false;
  while (el !== null && el !== doc.body && el !== doc.documentElement && levels.length < MAX_CLIMB) {
    // More than one video below this level means a sibling Reel is in it. Asking for the second
    // match stops the walk there; counting all of them walked the whole feed on every read
    // (docs/performance-report.md: 64% of the time of reading a Reel's metadata).
    if (el.getElementsByTagName(SEL.video)[1] !== undefined) {
      bounded = true;
      break;
    }
    levels.push(el);
    el = el.parentElement;
  }
  return { levels, bounded };
}

/** Most frequent value, first seen wins a tie. */
function mostFrequent(values: string[]): string | null {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v, (counts.get(v) ?? 0) + 1);
  let best: string | null = null;
  let bestCount = 0;
  for (const [v, n] of counts) {
    if (n > bestCount) {
      best = v;
      bestCount = n;
    }
  }
  return best;
}

function matchPaths(level: Element, pattern: RegExp): string[] {
  const out: string[] = [];
  for (const a of level.querySelectorAll(SEL.anchors)) {
    const path = pathOf(a);
    const m = path === null ? null : pattern.exec(path);
    if (m?.[1] !== undefined) out.push(decodeSafe(m[1]));
  }
  return out;
}

function decodeSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

// ---------- field extraction ----------

export function readShortcodeFromLinks(video: Element, doc: Document): Field<string> {
  for (const level of climb(video, doc).levels) {
    const codes = [...new Set(matchPaths(level, REEL_LINK))].filter((c) => !RESERVED.has(c));
    if (codes.length === 1 && codes[0] !== undefined) return available(codes[0], 'dom_link');
    if (codes.length > 1) return unavailable('ambiguous_links');
  }
  return unavailable('no_link');
}

export function readShortcodeFromUrl(href: string): Field<string> {
  let path: string;
  try {
    const url = new URL(href);
    if (url.origin !== ORIGIN) return unavailable('not_instagram');
    path = url.pathname;
  } catch {
    return unavailable('bad_url');
  }
  const code = URL_CODE.exec(path)?.[1];
  if (code === undefined || RESERVED.has(code)) return unavailable('no_code_in_url');
  return available(code, 'url');
}

function readHandle(video: Element, doc: Document): Field<string> {
  for (const level of climb(video, doc).levels) {
    const handles = matchPaths(level, PROFILE_LINK).filter(
      (h) => !RESERVED.has(h.toLowerCase()),
    );
    const best = mostFrequent(handles);
    if (best !== null) return available(best, 'profile_link');
  }
  for (const level of climb(video, doc).levels) {
    for (const img of level.querySelectorAll(SEL.avatar)) {
      const m = AVATAR_ALT.exec(img.getAttribute('alt') ?? '');
      if (m?.[1] !== undefined) return available(m[1], 'avatar_alt');
    }
  }
  return unavailable('no_profile_link');
}

interface CaptionRead {
  field: Field<string>;
  truncated: boolean;
}

function readCaption(video: Element, doc: Document, handle: Field<string>): CaptionRead {
  const handleText = handle.status === 'available' ? handle.value.toLowerCase() : null;
  for (const level of climb(video, doc).levels) {
    let field: Field<string> | null = null;

    const h1 = clean(level.querySelector(SEL.h1)?.textContent ?? null);
    if (h1 !== '') field = available(h1, 'h1');

    if (field === null) {
      let best = '';
      for (const node of level.querySelectorAll(SEL.userText)) {
        const text = clean(node.textContent);
        if (text === '' || text.toLowerCase() === handleText) continue;
        if (node.closest('a') !== null && !text.includes(' ')) continue; // a bare link label
        if (text.length > best.length) best = text;
      }
      if (best !== '') field = available(best, 'dir_auto');
    }

    if (field !== null) return { field, truncated: hasMoreControl(level) };
  }
  return { field: unavailable('no_caption_text'), truncated: false };
}

function hasMoreControl(level: Element): boolean {
  for (const el of level.querySelectorAll(SEL.moreControls)) {
    if (MORE_LABELS.has(clean(el.textContent).toLowerCase())) return true;
  }
  return false;
}

function readHashtags(video: Element, doc: Document, caption: Field<string>): Field<string[]> {
  const found: string[] = [];
  const seen = new Set<string>();
  const add = (tag: string) => {
    const key = tag.toLowerCase();
    if (tag !== '' && !seen.has(key)) {
      seen.add(key);
      found.push(tag);
    }
  };

  let linkTags = false;
  for (const level of climb(video, doc).levels) {
    const tags = matchPaths(level, TAG_LINK);
    if (tags.length > 0) {
      tags.forEach(add);
      linkTags = true;
      break;
    }
  }
  if (caption.status === 'available') {
    for (const m of caption.value.matchAll(HASHTAG_IN_TEXT)) add(m[1] ?? '');
  }

  if (found.length > 0) return available(found, linkTags ? 'tag_links' : 'caption_text');
  // A readable caption with no tag in it is a real answer; no caption and no links is not.
  if (caption.status === 'available') return available([], 'caption_text');
  return unavailable('no_tag_links_and_no_caption');
}

function readDuration(video: HTMLVideoElement): Field<number> {
  const seconds = video.duration;
  if (Number.isNaN(seconds)) return unavailable('duration_not_loaded');
  if (!Number.isFinite(seconds)) return unavailable('duration_unbounded');
  if (seconds <= 0) return unavailable('duration_zero');
  return available(Math.round(seconds * 1000), 'video_element');
}

/** Everything the page tells us about the reel that this video belongs to. */
export function readReel(env: AdapterEnv, video: HTMLVideoElement): ReelMetadata {
  const doc = env.doc;
  const fromLinks = readShortcodeFromLinks(video, doc);
  // The page URL names the reel in view; it is only a fallback, because it may lag the primary.
  const shortcode = fromLinks.status === 'available' ? fromLinks : readShortcodeFromUrl(env.href());
  const creator_handle = readHandle(video, doc);
  const caption = readCaption(video, doc, creator_handle);
  return {
    adapter_version: ADAPTER_VERSION,
    shortcode,
    creator_handle,
    caption: caption.field,
    caption_truncated: caption.truncated,
    hashtags: readHashtags(video, doc, caption.field),
    video_duration_ms: readDuration(video),
  };
}

export function readPlayback(env: AdapterEnv, video: HTMLVideoElement): PlaybackState {
  const state = env.doc.visibilityState;
  return {
    playing: video.isConnected
      ? available(!video.paused && !video.ended && video.readyState >= 3, 'video_element')
      : unavailable('video_detached'),
    tab_visible:
      state === 'visible' || state === 'hidden'
        ? available(state === 'visible', 'visibility_state')
        : unavailable('unknown_visibility_state'),
  };
}

// ---------- primary Reel detection ----------

export interface PrimaryTracker {
  start(): void;
  stop(): void;
  current(): HTMLVideoElement | null;
}

/**
 * Tracks <video> elements and reports the primary one: the video with at least 60% of itself
 * visible (spec F4), the largest visible area winning when several qualify. The current primary
 * keeps its place on a tie. Work per mutation batch is limited to the added and removed nodes.
 * `onChange` fires only when the primary actually changes (null when none qualifies).
 */
export function createPrimaryTracker(
  env: AdapterEnv,
  onChange: (video: HTMLVideoElement | null) => void,
): PrimaryTracker {
  const { IntersectionObserverCtor: IO, MutationObserverCtor: MO } = env;
  if (IO === undefined) throw new Error('IntersectionObserver is not available');

  const seen = new Map<Element, { ratio: number; area: number }>();
  let primary: HTMLVideoElement | null = null;
  let io: IntersectionObserver | null = null;
  let mo: MutationObserver | null = null;

  const recompute = () => {
    let best: HTMLVideoElement | null = null;
    let bestArea = -1;
    for (const [el, r] of seen) {
      if (!el.isConnected || r.ratio < PRIMARY_MIN_RATIO) continue;
      const better = r.area > bestArea || (r.area === bestArea && el === primary);
      if (better) {
        best = el as HTMLVideoElement;
        bestArea = r.area;
      }
    }
    if (best === primary) return;
    primary = best;
    onChange(primary);
  };

  const observe = (root: Element) => {
    const videos = root.matches(SEL.video) ? [root] : [...root.querySelectorAll(SEL.video)];
    for (const v of videos) {
      if (!seen.has(v)) {
        seen.set(v, { ratio: 0, area: 0 });
        io?.observe(v);
      }
    }
  };

  return {
    start() {
      if (io !== null) return;
      io = new IO(
        (entries) => {
          for (const e of entries) {
            if (!seen.has(e.target)) continue;
            seen.set(e.target, {
              ratio: e.intersectionRatio,
              area: e.intersectionRect.width * e.intersectionRect.height,
            });
          }
          recompute();
        },
        { threshold: [0, 0.25, 0.5, PRIMARY_MIN_RATIO, 0.75, 0.9, 1] },
      );
      for (const v of env.doc.getElementsByTagName(SEL.video)) observe(v);
      if (MO !== undefined) {
        mo = new MO((records) => {
          let removed = false;
          for (const r of records) {
            for (const n of r.addedNodes) if (n instanceof Element) observe(n);
            if (r.removedNodes.length > 0) removed = true;
          }
          if (removed) {
            for (const el of [...seen.keys()]) {
              if (!el.isConnected) {
                seen.delete(el);
                io?.unobserve(el);
              }
            }
            recompute();
          }
        });
        mo.observe(env.doc.documentElement, { childList: true, subtree: true });
      }
    },
    stop() {
      io?.disconnect();
      mo?.disconnect();
      io = null;
      mo = null;
      seen.clear();
      if (primary !== null) {
        primary = null;
        onChange(null);
      }
    },
    current: () => primary,
  };
}

// ---------- self-test ----------

export type CheckStatus = 'pass' | 'warn' | 'fail' | 'skipped';

export interface SelfTestCheck {
  id: string;
  assumption: string;
  status: CheckStatus;
  /** Which strategy worked, or why it did not. Never page text. */
  detail: string;
}

export interface SelfTestReport {
  adapter_version: string;
  /** No check failed. Warnings and skipped checks do not fail the report. */
  ok: boolean;
  /** Ids of failed checks: the selectors and assumptions that currently do not work. */
  failing: string[];
  checks: SelfTestCheck[];
}

const describeField = (field: Field<unknown>): string =>
  field.status === 'available' ? `via ${field.source}` : field.reason;

/**
 * Reports which assumptions hold on the page right now. `primary` is the video the tracker
 * reports; with none, the checks that need a video are skipped, not failed. No page text is put
 * in the report, so it is safe to log or show.
 */
export function runSelfTest(env: AdapterEnv, primary: HTMLVideoElement | null): SelfTestReport {
  const checks: SelfTestCheck[] = [];
  const add = (id: string, assumption: string, status: CheckStatus, detail: string) =>
    checks.push({ id, assumption, status, detail });

  add(
    'surface',
    'The address is https://www.instagram.com/reels/*',
    isReelsSurface(env.href()) ? 'pass' : 'fail',
    isReelsSurface(env.href()) ? 'reels surface' : 'not the reels surface',
  );
  add(
    'intersection_observer',
    'IntersectionObserver is available',
    env.IntersectionObserverCtor === undefined ? 'fail' : 'pass',
    env.IntersectionObserverCtor === undefined ? 'missing' : 'present',
  );
  const videoCount = env.doc.getElementsByTagName(SEL.video).length;
  add(
    'A1_video_elements',
    'A1: reels are <video> elements',
    videoCount > 0 ? 'pass' : 'fail',
    `${videoCount} video element(s)`,
  );

  if (primary === null) {
    for (const [id, assumption] of [
      ['primary_video', 'A reel is at least 60% visible'],
      ['A2_container', 'A2: metadata is in ancestors of the video'],
      ['A3_shortcode_link', 'A3: the reel has its own link'],
      ['A3_shortcode_url', 'A3: the address names the reel in view'],
      ['A3_shortcode_agree', 'A3: link and address name the same reel'],
      ['A4_creator_handle', 'A4: creator is a profile link'],
      ['A5_caption', 'A5: caption is an h1 or dir="auto" text'],
      ['A6_hashtags', 'A6: hashtags are tag links or caption text'],
      ['A7_more_control', 'A7: a truncated caption shows a more control'],
      ['video_duration', 'The video reports a finite duration'],
    ] as const) {
      add(id, assumption, 'skipped', 'no primary video yet');
    }
    return finish(checks);
  }

  const doc = env.doc;
  add('primary_video', 'A reel is at least 60% visible', 'pass', 'found');

  const { levels, bounded } = climb(primary, doc);
  add(
    'A2_container',
    'A2: metadata is in ancestors of the video',
    levels.length === 0 ? 'fail' : bounded ? 'pass' : 'warn',
    levels.length === 0
      ? 'the video has no usable ancestor'
      : bounded
        ? `${levels.length} level(s), bounded by the next video`
        : `${levels.length} level(s), not bounded by another video (page chrome may leak in)`,
  );

  const meta = readReel(env, primary);
  const fromLinks = readShortcodeFromLinks(primary, doc);
  const fromUrl = readShortcodeFromUrl(env.href());
  add('A3_shortcode_link', 'A3: the reel has its own link', fromLinks.status === 'available' ? 'pass' : 'fail', describeField(fromLinks));
  add('A3_shortcode_url', 'A3: the address names the reel in view', fromUrl.status === 'available' ? 'pass' : 'warn', describeField(fromUrl));
  if (fromLinks.status === 'available' && fromUrl.status === 'available') {
    const same = fromLinks.value === fromUrl.value;
    add('A3_shortcode_agree', 'A3: link and address name the same reel', same ? 'pass' : 'fail',
      same ? 'same' : 'differ: the address lags or the link is not this reel');
  } else {
    add('A3_shortcode_agree', 'A3: link and address name the same reel', 'skipped', 'needs both');
  }

  add('A4_creator_handle', 'A4: creator is a profile link', meta.creator_handle.status === 'available' ? 'pass' : 'fail', describeField(meta.creator_handle));
  add('A5_caption', 'A5: caption is an h1 or dir="auto" text', meta.caption.status === 'available' ? 'pass' : 'warn', describeField(meta.caption));
  add('A6_hashtags', 'A6: hashtags are tag links or caption text', meta.hashtags.status === 'available' ? 'pass' : 'warn', describeField(meta.hashtags));
  add('A7_more_control', 'A7: a truncated caption shows a more control', 'pass', meta.caption_truncated ? 'caption is truncated' : 'no more control seen');
  add('video_duration', 'The video reports a finite duration', meta.video_duration_ms.status === 'available' ? 'pass' : 'warn', describeField(meta.video_duration_ms));

  return finish(checks);
}

function finish(checks: SelfTestCheck[]): SelfTestReport {
  const failing = checks.filter((c) => c.status === 'fail').map((c) => c.id);
  return { adapter_version: ADAPTER_VERSION, ok: failing.length === 0, failing, checks };
}

/** Browser wiring: the one place the adapter touches real globals. */
export function browserEnv(): AdapterEnv {
  return {
    doc: document,
    href: () => location.href,
    IntersectionObserverCtor:
      typeof IntersectionObserver === 'undefined' ? undefined : IntersectionObserver,
    MutationObserverCtor: typeof MutationObserver === 'undefined' ? undefined : MutationObserver,
  };
}

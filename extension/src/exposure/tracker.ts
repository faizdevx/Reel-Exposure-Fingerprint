/**
 * Reel exposure tracking (spec F4, F6): one event per viewing instance.
 *
 * A viewing instance is one continuous stretch in which a video is the primary Reel (at least 60%
 * visible, see ig-adapter.ts). It starts when the video becomes primary and ends when the primary
 * changes (to another Reel or to none), on pagehide, or when tracking stops. Scrolling away and
 * back to the same Reel is a second viewing instance.
 *
 * Dwell is the time inside an instance in which ALL of these hold: the Reel is primary, the tab
 * is visible, the video is playing. A hidden tab or a pause stops the clock without ending the
 * instance; the clock resumes when both hold again. If the page cannot say whether the video is
 * playing or the tab visible, the clock does not run: dwell is never guessed upwards.
 *
 * Duplicates: an instance emits at most once (`emitted` flag), its event_id is fixed when it
 * starts, and the service worker queue is keyed by event_id, so a resend cannot add a second row.
 *
 * Durations use a monotonic clock, never the wall clock. Wall time is used only for observed_at.
 * Nothing here is durable: the service worker owns the queue.
 */

import {
  ADAPTER_VERSION,
  createPrimaryTracker,
  readPlayback,
  readReel,
  type AdapterEnv,
  type Field,
  type PrimaryTracker,
  type ReelMetadata,
} from '../ig-adapter';
import type { EventField, ExposureBody } from '../shared/exposure';

/** Re-read the page once, shortly after a Reel becomes primary: its text may load late. */
export const METADATA_REFRESH_MS = 1000;

/** Media events that can change "is the video playing". Listened to on the primary video only. */
const MEDIA_EVENTS = ['play', 'playing', 'pause', 'waiting', 'canplay', 'ended', 'stalled', 'emptied'];

export interface ExposureEvent {
  event_id: string;
  body: ExposureBody;
}

export interface ExposureDeps {
  env: AdapterEnv;
  /** Monotonic milliseconds (performance.now). Used for dwell. */
  now(): number;
  /** Wall-clock milliseconds since the epoch (Date.now). Used only for observed_at. */
  wallNow(): number;
  newId(): string;
  /** Hand over a finished event. Must not be relied on to succeed; failures are counted. */
  emit(event: ExposureEvent): void;
  /** Run `fn` once after `ms`. Returns a cancel function. */
  schedule(fn: () => void, ms: number): () => void;
}

export interface ExposureStats {
  emitted: number;
  /** Instances dropped because no source gave a shortcode: such an event cannot identify a Reel. */
  droppedNoShortcode: number;
  emitFailures: number;
}

export interface ExposureTracker {
  /** Begin watching. Safe to call twice. */
  start(): void;
  /** End the open instance (emitting it) and stop watching. */
  stop(): void;
  /** The page is going away: end and emit the open instance once, then stop watching. */
  pageHide(): void;
  /** The page came back from the back/forward cache: watch again with a fresh instance. */
  resume(): void;
  /** Whether a viewing instance is open. */
  isOpen(): boolean;
  stats(): ExposureStats;
}

interface Instance {
  id: string;
  video: HTMLVideoElement;
  observedAtMs: number;
  dwellMs: number;
  lastTick: number;
  counting: boolean;
  meta: ReelMetadata;
  cancelRefresh: () => void;
  detach: () => void;
}

// ---------- merging reads of the same instance ----------

/** A later available read replaces an earlier one; an unavailable read never erases a value. */
function latest<T>(older: Field<T>, newer: Field<T>): Field<T> {
  return newer.status === 'available' ? newer : older;
}

function mergeShortcode(older: Field<string>, newer: Field<string>): Field<string> {
  // The page address may lag the Reel in view, so a link in the Reel's own markup always wins.
  if (older.status === 'available' && older.source === 'dom_link') {
    return newer.status === 'available' && newer.source === 'dom_link' ? newer : older;
  }
  return latest(older, newer);
}

function merge(older: ReelMetadata, newer: ReelMetadata): ReelMetadata {
  return {
    adapter_version: newer.adapter_version,
    shortcode: mergeShortcode(older.shortcode, newer.shortcode),
    creator_handle: latest(older.creator_handle, newer.creator_handle),
    caption: latest(older.caption, newer.caption),
    caption_truncated:
      newer.caption.status === 'available' ? newer.caption_truncated : older.caption_truncated,
    hashtags: latest(older.hashtags, newer.hashtags),
    video_duration_ms: latest(older.video_duration_ms, newer.video_duration_ms),
  };
}

function toEventField<T>(field: Field<T>): EventField<T> {
  return field.status === 'available'
    ? { value: field.value, status: 'available' }
    : { value: null, status: 'unavailable' };
}

// ---------- the tracker ----------

export function createExposureTracker(deps: ExposureDeps): ExposureTracker {
  const { env } = deps;
  let instance: Instance | null = null;
  let primary: PrimaryTracker | null = null;
  let watching = false;
  const counters: ExposureStats = { emitted: 0, droppedNoShortcode: 0, emitFailures: 0 };

  const isCounting = (video: HTMLVideoElement): boolean => {
    const state = readPlayback(env, video);
    return (
      state.playing.status === 'available' &&
      state.playing.value &&
      state.tab_visible.status === 'available' &&
      state.tab_visible.value
    );
  };

  /** Bank the time since the last tick if the clock was running, then re-evaluate it. */
  const sync = () => {
    if (instance === null) return;
    const t = deps.now();
    if (instance.counting) instance.dwellMs += t - instance.lastTick;
    instance.lastTick = t;
    instance.counting = isCounting(instance.video);
  };

  const refresh = (inst: Instance) => {
    if (!inst.video.isConnected) return; // an unmounted Reel can no longer be read
    inst.meta = merge(inst.meta, readReel(env, inst.video));
  };

  const finalize = () => {
    const inst = instance;
    if (inst === null) return;
    instance = null; // first, so a re-entrant call cannot emit this instance twice

    // Bank the last stretch. State is read as it is now: the primary just changed or the page is
    // leaving, and a Reel that is no longer primary does not count.
    const t = deps.now();
    if (inst.counting) inst.dwellMs += t - inst.lastTick;
    inst.cancelRefresh();
    inst.detach();
    refresh(inst);

    const shortcode = inst.meta.shortcode;
    if (shortcode.status !== 'available') {
      counters.droppedNoShortcode += 1;
      return;
    }
    const duration = inst.meta.video_duration_ms;
    const event: ExposureEvent = {
      event_id: inst.id,
      body: {
        reel_shortcode: shortcode.value,
        observed_at: new Date(inst.observedAtMs).toISOString(),
        dwell_ms: Math.max(0, Math.round(inst.dwellMs)),
        video_duration_ms: duration.status === 'available' ? duration.value : null,
        video_duration_status: duration.status,
        creator_handle: toEventField(inst.meta.creator_handle),
        caption: toEventField(inst.meta.caption),
        caption_truncated: inst.meta.caption_truncated,
        hashtags: toEventField(inst.meta.hashtags),
        frame_count: 0, // frame capture arrives in M4
        adapter_version: ADAPTER_VERSION,
      },
    };
    try {
      deps.emit(event);
      counters.emitted += 1;
    } catch {
      counters.emitFailures += 1;
    }
  };

  const begin = (video: HTMLVideoElement) => {
    const onMedia = () => sync();
    for (const name of MEDIA_EVENTS) video.addEventListener(name, onMedia, { passive: true });
    const inst: Instance = {
      id: deps.newId(),
      video,
      observedAtMs: deps.wallNow(),
      dwellMs: 0,
      lastTick: deps.now(),
      counting: isCounting(video),
      meta: readReel(env, video),
      cancelRefresh: () => undefined,
      detach: () => {
        for (const name of MEDIA_EVENTS) video.removeEventListener(name, onMedia);
      },
    };
    inst.cancelRefresh = deps.schedule(() => {
      if (instance === inst) refresh(inst);
    }, METADATA_REFRESH_MS);
    instance = inst;
  };

  const onPrimaryChange = (video: HTMLVideoElement | null) => {
    finalize();
    if (video !== null) begin(video);
  };

  const onVisibility = () => sync();

  const startWatching = () => {
    if (watching) return;
    watching = true;
    primary = createPrimaryTracker(env, onPrimaryChange);
    env.doc.addEventListener('visibilitychange', onVisibility);
    primary.start();
  };

  const stopWatching = () => {
    if (!watching) return;
    watching = false;
    finalize();
    env.doc.removeEventListener('visibilitychange', onVisibility);
    primary?.stop();
    primary = null;
  };

  return {
    start: startWatching,
    stop: stopWatching,
    pageHide: stopWatching,
    resume: startWatching,
    isOpen: () => instance !== null,
    stats: () => ({ ...counters }),
  };
}

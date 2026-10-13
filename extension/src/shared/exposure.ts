/**
 * The exposure event: one per viewing instance of a Reel (spec §3 F4, §8 event payload).
 * Typed on both sides of the content script to service worker message. The service worker adds
 * `session_id`; the content script must not send it.
 */

export type FieldStatus = 'available' | 'unavailable';

/** Spec §8 shape: `value` is null exactly when the status is `unavailable`. */
export type EventField<T> =
  | { value: T; status: 'available' }
  | { value: null; status: 'unavailable' };

export interface ExposureBody {
  reel_shortcode: string;
  /** ISO time the Reel became primary. Spec ADR-011 orders by this, then event_id. */
  observed_at: string;
  /** Milliseconds as primary while the tab was visible and the video playing. */
  dwell_ms: number;
  /** Null when the browser could not report it; see `video_duration_status`. */
  video_duration_ms: number | null;
  video_duration_status: FieldStatus;
  creator_handle: EventField<string>;
  caption: EventField<string>;
  /** True when the page showed a "more" control: the caption is cut short. */
  caption_truncated: boolean;
  hashtags: EventField<string[]>;
  /** Frames captured for this viewing. Frame capture arrives in M4, so this is 0 until then. */
  frame_count: number;
  adapter_version: string;
}

export const BODY_KEYS: readonly string[] = [
  'reel_shortcode',
  'observed_at',
  'dwell_ms',
  'video_duration_ms',
  'video_duration_status',
  'creator_handle',
  'caption',
  'caption_truncated',
  'hashtags',
  'frame_count',
  'adapter_version',
];

const MAX_DWELL_MS = 24 * 60 * 60 * 1000;
const MAX_FRAMES = 5;

function isField(value: unknown, valueOk: (v: unknown) => boolean): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const f = value as Record<string, unknown>;
  if (Object.keys(f).length !== 2) return false;
  if (f['status'] === 'available') return valueOk(f['value']);
  return f['status'] === 'unavailable' && f['value'] === null;
}

const isString = (v: unknown): boolean => typeof v === 'string';
const isInt = (v: unknown, max: number): boolean =>
  typeof v === 'number' && Number.isInteger(v) && v >= 0 && v <= max;

/** Strict: exact key set, right types, and `value` null exactly when `unavailable`. */
export function isExposureBody(value: unknown): value is ExposureBody {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const b = value as Record<string, unknown>;
  const keys = Object.keys(b);
  if (keys.length !== BODY_KEYS.length || !BODY_KEYS.every((k) => keys.includes(k))) return false;

  const shortcode = b['reel_shortcode'];
  if (typeof shortcode !== 'string' || shortcode === '' || shortcode.length > 64) return false;
  const observed = b['observed_at'];
  if (typeof observed !== 'string' || Number.isNaN(Date.parse(observed))) return false;
  if (!isInt(b['dwell_ms'], MAX_DWELL_MS)) return false;

  const status = b['video_duration_status'];
  const duration = b['video_duration_ms'];
  if (status === 'available' ? !isInt(duration, MAX_DWELL_MS) : !(status === 'unavailable' && duration === null)) {
    return false;
  }

  return (
    isField(b['creator_handle'], isString) &&
    isField(b['caption'], isString) &&
    isField(b['hashtags'], (v) => Array.isArray(v) && v.every(isString)) &&
    typeof b['caption_truncated'] === 'boolean' &&
    isInt(b['frame_count'], MAX_FRAMES) &&
    typeof b['adapter_version'] === 'string' &&
    b['adapter_version'] !== ''
  );
}

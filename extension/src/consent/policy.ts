import { API_BASE } from '../config';

/**
 * The consent text, as data, so the popup renders exactly what is recorded.
 *
 * Changing ANY wording below changes what users agreed to: bump POLICY_VERSION too.
 * test/policy.test.ts fails if the text changes while the version does not.
 */
export const POLICY_VERSION = '2026-10-07.1';

export interface Policy {
  intro: string;
  collected: readonly string[];
  derived: readonly string[];
  never: readonly string[];
  when: readonly string[];
  control: readonly string[];
}

export const POLICY: Policy = {
  intro:
    'This research tool records which Instagram Reels you watch, to build a picture of what ' +
    'the feed shows you and what you linger on. It only runs when you start it.',
  collected: [
    'The Reel ID (the code in its address).',
    'The creator handle, caption and hashtags shown with the Reel. Anything it cannot find is marked unavailable.',
    'When you watched it, how long it was on screen while the tab was visible and the video playing, and the video length.',
    'Up to 5 small still images from each Reel. They are deleted within 24 hours.',
  ],
  derived: [
    'Text read from those images, numeric summaries of the text and images, and topic scores. These are computed on your own server.',
  ],
  never: [
    'Your Instagram password, cookies or login tokens.',
    'Anything from other websites or from other parts of Instagram.',
    'Likes, comments, messages or audio.',
    'Anything at all while monitoring is off.',
  ],
  when: [
    'Only after you press Start, and only on instagram.com/reels.',
    'It stops when you press Stop, when you leave Reels for 60 seconds, when you close the tab, or after 60 minutes.',
    `Data is sent only to the REF server at ${API_BASE}.`,
  ],
  control: [
    'You can revoke consent at any time. Collection stops at once and anything not yet sent is deleted from this browser.',
    'Data already stored on the server stays there until you delete it. Deleting it from the dashboard is not available yet.',
  ],
};

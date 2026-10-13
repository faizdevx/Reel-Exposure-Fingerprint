import { describe, expect, it } from 'vitest';

import { isReelsSurface } from '../src/session/surface';

describe('isReelsSurface', () => {
  it.each([
    'https://www.instagram.com/reels/',
    'https://www.instagram.com/reels',
    'https://www.instagram.com/reels/Cabc123/',
    'https://www.instagram.com/reels/Cabc123/?igsh=x#top',
  ])('accepts %s', (url) => {
    expect(isReelsSurface(url)).toBe(true);
  });

  it.each([
    undefined,
    '',
    'not a url',
    'https://www.instagram.com/',
    'https://www.instagram.com/explore/',
    'https://www.instagram.com/reel/Cabc123/', // single-Reel page: not in the spec's /reels/* (A-2)
    'https://www.instagram.com/reelsfake/',
    'https://www.instagram.com/someone/reels/',
    'http://www.instagram.com/reels/',
    'https://instagram.com/reels/',
    'https://www.instagram.com.evil.test/reels/',
    'https://evil.test/www.instagram.com/reels/',
    'https://evil.test/?u=https://www.instagram.com/reels/',
    'chrome://extensions/',
  ])('rejects %s', (url) => {
    expect(isReelsSurface(url)).toBe(false);
  });
});

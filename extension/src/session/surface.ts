/**
 * Is this URL the Reels surface? Spec F3: https://www.instagram.com/reels/*.
 *
 * Instagram also serves single Reels under /reel/<code>/. The spec names only /reels/*, so
 * /reel/ is not treated as the surface here; M0 experiment A-2 decides whether to widen it.
 * (DOM selectors live in ig-adapter.ts, not here: this reads only the address.)
 */
export function isReelsSurface(url: string | undefined): boolean {
  if (url === undefined) return false; // the browser hides non-instagram.com tab URLs from us
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === 'https:' &&
    parsed.hostname === 'www.instagram.com' &&
    (parsed.pathname === '/reels' || parsed.pathname.startsWith('/reels/'))
  );
}

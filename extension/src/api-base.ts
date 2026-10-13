/**
 * Where the REF server is, and what the extension may say to it (audit H-4, spec N6).
 *
 * The address is fixed when the extension is built (`VITE_REF_API_BASE`), not typed into the popup:
 * a cohort gets an extension built for its server, and the manifest asks for host access to that
 * server only. Plain HTTP is accepted for this computer alone (127.0.0.1, localhost, ::1); every
 * other server must be HTTPS, so the Bearer token never crosses a network in clear text.
 *
 * This file imports nothing, so the manifest (run by Node at build time) and the extension share it.
 */

export const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);

/** The address as an origin without a trailing slash, or an Error saying why it is refused. */
export function validateApiBase(value: string | undefined): string {
  const text = (value ?? '').trim() === '' ? DEFAULT_API_BASE : (value ?? '').trim();
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new Error(`REF server address is not a URL: ${JSON.stringify(text)}`);
  }
  if (url.username !== '' || url.password !== '') {
    throw new Error('REF server address must not contain credentials');
  }
  if (url.search !== '' || url.hash !== '' || (url.pathname !== '/' && url.pathname !== '')) {
    throw new Error('REF server address must be an origin only: scheme, host and port');
  }
  if (url.protocol === 'https:') return url.origin;
  if (url.protocol === 'http:' && LOOPBACK.has(url.hostname)) return url.origin;
  throw new Error(
    'REF server address must use https, except for 127.0.0.1, localhost or ::1 on this computer',
  );
}

/** Chrome match pattern for the server (match patterns ignore the port). */
export function hostPermissionFor(base: string): string {
  const url = new URL(base);
  return `${url.protocol}//${url.hostname}/*`;
}

/** Host access the extension asks for: Instagram, and the REF server it was built for. */
export function hostPermissions(base: string): string[] {
  const own = hostPermissionFor(base);
  const local = LOOPBACK.has(new URL(base).hostname);
  // The default build keeps both loopback names (127.0.0.1 and localhost are one computer); a build
  // for a remote server asks for that server alone.
  const servers = local ? ['http://127.0.0.1/*', 'http://localhost/*'] : [own];
  return ['https://www.instagram.com/*', ...servers];
}

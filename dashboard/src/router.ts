import { useEffect, useState } from 'react';

export type Route = '/' | '/sessions' | '/history' | '/account' | '/login' | 'not-found';

export function routeFor(pathname: string): Route {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (path === '/' || path === '') return '/';
  if (path === '/sessions') return '/sessions';
  if (path === '/history') return '/history';
  if (path === '/account') return '/account';
  if (path === '/login') return '/login';
  return 'not-found';
}

const EVENT = 'ref:navigate';

/** Client-side navigation without a router dependency: three pages do not need one. */
export function navigate(path: string, replace = false): void {
  if (window.location.pathname === path) return;
  if (replace) window.history.replaceState(null, '', path);
  else window.history.pushState(null, '', path);
  window.dispatchEvent(new Event(EVENT));
}

export function usePathname(): string {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener('popstate', update);
    window.addEventListener(EVENT, update);
    return () => {
      window.removeEventListener('popstate', update);
      window.removeEventListener(EVENT, update);
    };
  }, []);
  return pathname;
}

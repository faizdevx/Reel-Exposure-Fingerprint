import { useQuery, useQueryClient } from '@tanstack/react-query';
import { type MouseEvent, type ReactNode, useState } from 'react';

import { fetchHealth, logout } from './api';
import { navigate } from './router';

function NavLink({ to, current, children }: { to: string; current: boolean; children: ReactNode }) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    // Let the browser handle new-tab clicks; handle plain clicks without a page load.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(to);
  }
  return (
    <a href={to} onClick={onClick} aria-current={current ? 'page' : undefined}>
      {children}
    </a>
  );
}

function ServerStatus() {
  const { data, isPending, isError } = useQuery({
    queryKey: ['healthz'],
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: 30_000,
  });
  if (isPending) return <span>checking…</span>;
  if (isError || data.status !== 'ok') return <span role="alert">server unreachable</span>;
  return <span>server ok</span>;
}

export function Layout({ path, children }: { path: string; children: ReactNode }) {
  const queryClient = useQueryClient();
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutFailed, setLogoutFailed] = useState(false);

  async function onLogout() {
    setLoggingOut(true);
    setLogoutFailed(false);
    try {
      await logout();
    } catch {
      // A failed logout call must not leave the user thinking they are out: say so.
      setLoggingOut(false);
      setLogoutFailed(true);
      return;
    }
    queryClient.clear();
    setLoggingOut(false);
    navigate('/login');
  }

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">Reel Exposure Fingerprint</span>
        <nav aria-label="Main">
          <NavLink to="/" current={path === '/'}>
            Dashboard
          </NavLink>
          <NavLink to="/history" current={path === '/history'}>
            History
          </NavLink>
          <NavLink to="/sessions" current={path === '/sessions'}>
            Sessions
          </NavLink>
          <NavLink to="/account" current={path === '/account'}>
            Account
          </NavLink>
        </nav>
        <button type="button" className="link" onClick={() => void onLogout()} disabled={loggingOut}>
          Log out
        </button>
      </header>
      {logoutFailed ? (
        <p role="alert" className="notice error">
          Could not log out. Try again.
        </p>
      ) : null}
      <main>{children}</main>
      <footer className="footer">
        Server: <ServerStatus />
      </footer>
    </div>
  );
}

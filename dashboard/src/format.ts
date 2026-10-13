const DATE_TIME = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? 'unknown time' : DATE_TIME.format(date);
}

/** "12 min", "1 h 05 min", or "less than a minute"; null when the session has no end yet. */
export function formatDuration(startIso: string, endIso: string | null): string | null {
  if (endIso === null) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return 'less than a minute';
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`;
}

const STOP_REASONS: Record<string, string> = {
  user: 'Stopped by you',
  left_surface: 'Left Reels',
  tab_closed: 'Tab closed',
  time_cap: '60 minute limit',
  browser_restart: 'Browser restarted',
  consent_revoked: 'Consent revoked',
};

export function describeSessionStatus(status: string, stopReason: string | null): string {
  if (status === 'active') return 'Running';
  if (stopReason === null) return 'Stopped';
  return STOP_REASONS[stopReason] ?? 'Stopped';
}

import { DeletionIncompleteError, type DeletionReport } from './api';

const LABELS: Record<string, string> = {
  sessions: 'sessions',
  reel_exposures: 'watched Reels',
  jobs: 'processing jobs',
  predictions: 'predictions',
  topic_transitions: 'topic transitions',
  fingerprint_snapshots: 'fingerprint snapshots',
  content_features: 'cached Reel features',
  topic_assignments: 'cached topic scores',
  frame_files: 'stored frame files',
  extension_tokens: 'extension tokens',
  consents: 'consent records',
  user_sessions: 'logins',
  invites: 'invites',
  users: 'account',
};

/** What a finished deletion removed, in words. Counts only. */
export function describeDeleted(report: DeletionReport): string {
  const parts = Object.entries(report.deleted)
    .filter(([, n]) => n > 0)
    .map(([table, n]) => `${n} ${LABELS[table] ?? table}`);
  if (report.frame_files_deleted > 0) parts.push(`${report.frame_files_deleted} frame files`);
  return parts.length > 0 ? parts.join(', ') : 'nothing: it was already gone';
}

/** The verification, in words: what is left after the deletion (must be nothing). */
export function describeRemaining(report: DeletionReport): string {
  const left = Object.entries(report.remaining)
    .filter(([, n]) => n > 0)
    .map(([table, n]) => `${n} ${LABELS[table] ?? table}`);
  return left.length > 0 ? left.join(', ') : 'nothing left in any table or on disk';
}

/** Shown after a deletion call: success only when the server's own verification found nothing. */
export function DeletionResult({ report }: { report: DeletionReport }) {
  return (
    <p role="status" className="notice done">
      Deleted: {describeDeleted(report)}. Verified: {describeRemaining(report)}.
    </p>
  );
}

export function DeletionFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const incomplete = error instanceof DeletionIncompleteError;
  return (
    <div role="alert" className="notice error">
      <p>
        {incomplete
          ? `The deletion is not finished: ${describeRemaining(error.report)} is still there. Nothing is lost by trying again.`
          : 'Could not delete. Check your connection and try again.'}
      </p>
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

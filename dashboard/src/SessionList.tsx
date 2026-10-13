import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { deleteSession, type DeletionReport, fetchSessions, type SessionSummary } from './api';
import { DeletionFailure, DeletionResult } from './DeletionNotice';
import { describeSessionStatus, formatDateTime, formatDuration } from './format';

/** Everything that was derived from sessions has to be read again after one is deleted. */
const AFFECTED = [['sessions'], ['fingerprint'], ['history'], ['evidence']] as const;

function SessionItem({ session, onDeleted }: { session: SessionSummary; onDeleted: (r: DeletionReport) => void }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const mutation = useMutation({
    mutationFn: () => deleteSession(session.id),
    onSuccess: async (report) => {
      await Promise.all(AFFECTED.map((key) => queryClient.invalidateQueries({ queryKey: key })));
      onDeleted(report);
    },
  });
  const duration = formatDuration(session.started_at, session.ended_at);
  const reels = session.n_reels === null ? 'its Reels' : `its ${session.n_reels} Reels`;
  return (
    <li className="session">
      <div>
        <span className="label">Started</span>
        <span>{formatDateTime(session.started_at)}</span>
      </div>
      <div>
        <span className="label">Length</span>
        <span>{duration ?? (session.status === 'active' ? 'in progress' : 'unknown')}</span>
      </div>
      <div>
        <span className="label">Reels</span>
        <span>{session.n_reels === null ? 'not processed yet' : session.n_reels}</span>
      </div>
      <div>
        <span className="label">Status</span>
        <span>{describeSessionStatus(session.status, session.stop_reason)}</span>
      </div>
      <div className="session-actions">
        {confirming ? (
          <div role="group" aria-label="Confirm deleting this session" className="confirm">
            <p>
              Delete this session? This removes {reels}, their stored frames and everything derived from
              them, and recomputes your fingerprints without it. It cannot be undone.
            </p>
            {mutation.isError ? (
              <DeletionFailure error={mutation.error} onRetry={() => mutation.mutate()} />
            ) : null}
            <button type="button" className="danger" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? 'Deleting…' : 'Yes, delete this session'}
            </button>
            <button type="button" className="link" disabled={mutation.isPending} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="link"
            onClick={() => setConfirming(true)}
            aria-label={`Delete the session of ${formatDateTime(session.started_at)}`}
          >
            Delete session
          </button>
        )}
      </div>
    </li>
  );
}

export function SessionList() {
  const [lastDeleted, setLastDeleted] = useState<DeletionReport | null>(null);
  const query = useQuery({
    queryKey: ['sessions'],
    queryFn: ({ signal }) => fetchSessions(signal),
  });

  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" className="skeleton">
        <span className="sr-only">Loading sessions…</span>
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="notice error">
        <p>Could not load your sessions.</p>
        <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>
          Try again
        </button>
      </div>
    );
  }
  const notice = lastDeleted ? <DeletionResult report={lastDeleted} /> : null;
  if (query.data.length === 0) {
    return (
      <>
        {notice}
        <p className="notice empty">No sessions yet. Start one from the browser extension while watching Reels.</p>
      </>
    );
  }
  return (
    <>
      {notice}
      <ul className="sessions" aria-label="Monitoring sessions">
        {query.data.map((session) => (
          <SessionItem key={session.id} session={session} onDeleted={setLastDeleted} />
        ))}
      </ul>
    </>
  );
}

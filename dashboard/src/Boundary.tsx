import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';

interface Props {
  queries: readonly UseQueryResult<unknown>[];
  /** What this section is called, for messages: "the topic share chart". */
  what: string;
  /** Text shown instead of the content when there is nothing to show, or null when there is. */
  empty: () => string | null;
  children: () => ReactNode;
}

/** Loading, error (with retry) and empty states in one place, for sections that read the API. */
export function Boundary({ queries, what, empty, children }: Props) {
  if (queries.some((q) => q.isError)) {
    const failed = queries.filter((q) => q.isError);
    return (
      <div role="alert" className="notice error">
        <p>Could not load {what}.</p>
        <button
          type="button"
          onClick={() => failed.forEach((q) => void q.refetch())}
          disabled={failed.some((q) => q.isFetching)}
        >
          Try again
        </button>
      </div>
    );
  }
  if (queries.some((q) => q.isPending)) {
    return (
      <div role="status" aria-live="polite" className="skeleton">
        <span className="sr-only">Loading {what}…</span>
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
      </div>
    );
  }
  const message = empty();
  if (message !== null) return <p className="notice empty">{message}</p>;
  return <>{children()}</>;
}

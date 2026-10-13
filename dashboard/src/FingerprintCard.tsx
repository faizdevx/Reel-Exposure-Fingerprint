import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { type FingerprintKind, fetchFingerprint } from './api';
import { EvidenceDrawer } from './EvidenceDrawer';
import { FingerprintChart } from './FingerprintChart';
import { formatDateTime } from './format';
import { type ShareRow, toShareRows } from './shares';

interface Props {
  kind: FingerprintKind;
  title: string;
  blurb: string;
  color: string;
}

export function FingerprintCard({ kind, title, blurb, color }: Props) {
  const query = useQuery({
    queryKey: ['fingerprint', kind],
    queryFn: ({ signal }) => fetchFingerprint(kind, signal),
  });
  const headingId = `${kind}-heading`;
  const [selected, setSelected] = useState<ShareRow | null>(null);

  let body;
  if (query.isPending) {
    body = (
      <div role="status" aria-live="polite" className="skeleton">
        <span className="sr-only">Loading {title.toLowerCase()}…</span>
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
      </div>
    );
  } else if (query.isError) {
    body = (
      <div role="alert" className="notice error">
        <p>Could not load the {title.toLowerCase()}.</p>
        <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>
          Try again
        </button>
      </div>
    );
  } else if (query.data.topics.length === 0) {
    body = (
      <p className="notice empty">
        No {kind} fingerprint yet. It appears after a monitoring session has been processed.
      </p>
    );
  } else {
    const { data } = query;
    body = (
      <>
        <p className="meta">
          {data.n_reels} {data.n_reels === 1 ? 'Reel' : 'Reels'}
          {data.taken_at ? ` · updated ${formatDateTime(data.taken_at)}` : ''}
        </p>
        <p className="meta">Select a topic to see which Reels put it there.</p>
        <FingerprintChart
          title={title}
          rows={toShareRows(data.topics)}
          color={color}
          onSelect={setSelected}
        />
      </>
    );
  }

  return (
    <section className="card" aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      <p className="blurb">{blurb}</p>
      {body}
      {selected && selected.topicId !== null ? (
        <EvidenceDrawer
          kind={kind}
          topic={{ id: selected.topicId, label: selected.label }}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </section>
  );
}

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { type EvidenceReason, type EvidenceReel, fetchEvidence, type FingerprintKind } from './api';
import { formatDateTime } from './format';
import { formatPercent } from './shares';

export const PAGE_SIZE = 10;
const TOP_N = 5;

interface Props {
  kind: FingerprintKind;
  topic: { id: number; label: string };
  onClose: () => void;
}

const REASONS: Record<EvidenceReason, string> = {
  no_fingerprint: 'There is no fingerprint yet, so there is nothing to explain.',
  topic_not_in_fingerprint: 'No Reel has put any weight on this topic, so it is not part of the fingerprint.',
  snapshot_without_provenance:
    'This fingerprint was saved before the evidence was recorded. Rebuild it to see the Reels behind it.',
};

const score = (p: number) => formatPercent(p * 100);
const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function describeDwell(dwell: number | null, duration: number | null): string {
  if (dwell === null) return 'not recorded';
  return duration === null ? `${seconds(dwell)} (length not recorded)` : `${seconds(dwell)} of ${seconds(duration)}`;
}

function ReelRow({ item, kind }: { item: EvidenceReel; kind: FingerprintKind }) {
  return (
    <li className="evidence-item">
      <div className="evidence-head">
        <a href={item.reel_url} target="_blank" rel="noopener noreferrer">
          Open Reel {item.reel_shortcode}
        </a>
        <time dateTime={item.observed_at}>Observed {formatDateTime(item.observed_at)}</time>
      </div>
      {item.caption === null ? (
        <p className="caption muted">No caption was captured for this Reel.</p>
      ) : (
        <p className="caption">
          {item.caption}
          {item.caption_truncated ? '…' : ''}
        </p>
      )}
      <dl className="evidence-numbers">
        <div>
          <dt>Topic score</dt>
          <dd>
            {score(item.topic_score)}
            {item.p_text !== null || item.p_visual !== null ? (
              <span className="sub">
                {item.p_text !== null ? ` text ${score(item.p_text)}` : ''}
                {item.p_visual !== null ? ` visual ${score(item.p_visual)}` : ''}
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Dwell</dt>
          <dd>{describeDwell(item.dwell_ms, item.video_duration_ms)}</dd>
        </div>
        <div>
          <dt>{kind === 'engagement' ? 'Gain (by dwell)' : 'Gain'}</dt>
          <dd>{item.gain.toFixed(2)}</dd>
        </div>
        <div>
          <dt>Recency</dt>
          <dd>{item.recency.toFixed(2)}</dd>
        </div>
        <div>
          <dt>Contribution</dt>
          <dd>{item.contribution.toFixed(3)}</dd>
        </div>
        <div>
          <dt>Share of this topic</dt>
          <dd>{score(item.share_of_topic)}</dd>
        </div>
      </dl>
      <div className="share-bar" aria-hidden="true">
        <span style={{ width: `${Math.min(100, item.share_of_topic * 100)}%` }} />
      </div>
    </li>
  );
}

/**
 * Why a topic is in a fingerprint: the Reels that put it there, biggest first, with the three
 * factors of each contribution (topic score, gain, recency). Their contributions add up to the
 * topic's weight, which the summary states.
 */
export function EvidenceDrawer({ kind, topic, onClose }: Props) {
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ['evidence', kind, topic.id, offset],
    queryFn: ({ signal }) => fetchEvidence(kind, topic.id, PAGE_SIZE, offset, signal),
    placeholderData: keepPreviousData,
  });
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const opener = useRef<Element | null>(null);

  // A new page starts at the top of the panel, not where the last one was scrolled to.
  useEffect(() => {
    panelRef.current?.scrollTo?.({ top: 0 });
  }, [offset]);

  // The latest onClose, without re-running the focus effect when the parent re-renders.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    opener.current = document.activeElement;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCloseRef.current();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, []);

  const data = query.data;
  const fp = data?.fingerprint ?? null;
  const topFive = offset === 0 && data ? data.items.slice(0, TOP_N) : [];
  const topShare = topFive.reduce((sum, item) => sum + item.share_of_topic, 0);

  let body;
  if (query.isError && !data) {
    body = (
      <div role="alert" className="notice error">
        <p>Could not load the evidence for {topic.label}.</p>
        <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>
          Try again
        </button>
      </div>
    );
  } else if (!data) {
    body = (
      <div role="status" aria-live="polite" className="skeleton">
        <span className="sr-only">Loading the evidence for {topic.label}…</span>
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
        <div className="skeleton-bar" />
      </div>
    );
  } else if (!data.evidence_available || !fp || !data.explanation) {
    body = <p className="notice empty">{data.reason ? REASONS[data.reason] : 'No evidence is available.'}</p>;
  } else {
    const end = data.offset + data.items.length;
    body = (
      <>
        <section className="evidence-summary" aria-label="Why this topic appears">
          <p>
            <strong>{topic.label}</strong> is <strong>{formatPercent(fp.topic_share * 100)}</strong> of your{' '}
            {kind} fingerprint: a weight of {fp.topic_weight.toFixed(2)} out of {fp.total_weight.toFixed(2)} in
            total, built from {fp.n_reels} Reels.
          </p>
          <p>{data.explanation.gain_rule}</p>
          <p>
            {data.explanation.formula} The half-life is {fp.half_life_days} days.
          </p>
          <p data-testid="coverage">
            {data.explanation.contributing_reels} Reels contribute, and together they account for{' '}
            {formatPercent(data.explanation.coverage * 100)} of this topic&apos;s weight.
            {topFive.length > 1
              ? ` The ${topFive.length} biggest contributors account for ${formatPercent(topShare * 100)}.`
              : ''}
          </p>
        </section>
        <ol className="evidence-list" aria-label={`Reels behind ${topic.label}, biggest contribution first`}>
          {data.items.map((item) => (
            <ReelRow key={item.event_id} item={item} kind={kind} />
          ))}
        </ol>
        <nav className="pager" aria-label="Evidence pages">
          <span data-testid="range">
            {data.items.length === 0 ? 'No Reels on this page' : `Showing ${data.offset + 1}–${end} of ${data.total}`}
          </span>
          <button
            type="button"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0 || query.isFetching}
          >
            Previous
          </button>
          <button
            type="button"
            onClick={() => data.next_offset !== null && setOffset(data.next_offset)}
            disabled={data.next_offset === null || query.isFetching}
          >
            Next
          </button>
        </nav>
      </>
    );
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="drawer"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="evidence-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-head">
          <h2 id="evidence-title">
            Why {topic.label} is in your {kind} fingerprint
          </h2>
          <button type="button" className="link" onClick={onClose} ref={closeRef}>
            Close
          </button>
        </header>
        <div className={query.isPlaceholderData ? 'drawer-body stale' : 'drawer-body'}>{body}</div>
      </aside>
    </div>
  );
}

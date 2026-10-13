import { useState } from 'react';

import type { FingerprintKind } from './api';
import { DailySnapshots } from './DailySnapshots';
import { ExposureVsEngagement } from './ExposureVsEngagement';
import { KindToggle } from './KindToggle';
import { SessionTimeline } from './SessionTimeline';
import { ShareOverTime } from './ShareOverTime';

/** Everything on this page is read from stored snapshots: no history is computed on request. */
export function HistoryPage() {
  const [kind, setKind] = useState<FingerprintKind>('exposure');
  return (
    <>
      <h1>History</h1>
      <p className="lead">
        How your fingerprints changed over time. Each day with Reels has one saved snapshot, and each
        session has one too.
      </p>
      <div className="filters">
        <KindToggle value={kind} onChange={setKind} />
      </div>

      <section className="card block" aria-labelledby="share-heading">
        <h2 id="share-heading">Topic share over time</h2>
        <p className="blurb">
          Each day is one column: how the {kind} fingerprint was split across topics at the end of
          that day. Every column adds up to 100%.
        </p>
        <ShareOverTime kind={kind} />
      </section>

      <section className="card block" aria-labelledby="compare-heading">
        <h2 id="compare-heading">Exposure versus engagement</h2>
        <p className="blurb">The feed's mix against where your time went, now.</p>
        <ExposureVsEngagement />
      </section>

      <section className="card block" aria-labelledby="snapshots-heading">
        <h2 id="snapshots-heading">Fingerprint history</h2>
        <p className="blurb">The saved daily snapshots of the {kind} fingerprint, newest first.</p>
        <DailySnapshots kind={kind} />
      </section>

      <section className="card block" aria-labelledby="timeline-heading">
        <h2 id="timeline-heading">Session timeline</h2>
        <p className="blurb">Your monitoring sessions in time order.</p>
        <SessionTimeline />
      </section>
    </>
  );
}

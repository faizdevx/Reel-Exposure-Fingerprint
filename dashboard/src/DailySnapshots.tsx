import { useQuery } from '@tanstack/react-query';

import { type FingerprintKind, fetchHistory } from './api';
import { Boundary } from './Boundary';
import { leadingTopic, utcDay } from './series';
import { DAYS } from './ShareOverTime';

/** One row per day on which Reels were watched, newest first, read from the daily snapshots. */
export function DailySnapshots({ kind }: { kind: FingerprintKind }) {
  const query = useQuery({
    queryKey: ['history', 'day', kind],
    queryFn: ({ signal }) => fetchHistory(kind, 'day', DAYS, signal),
  });
  return (
    <Boundary
      queries={[query]}
      what="the daily snapshots"
      empty={() =>
        (query.data?.points.length ?? 0) === 0
          ? `No ${kind} snapshots yet. One is saved for each day you watched Reels.`
          : null
      }
    >
      {() => {
        const history = query.data;
        if (!history) return null;
        const points = [...history.points].reverse();
        return (
          <div className="scroll">
            <table className="shares wide" aria-label={`${kind} daily snapshots`}>
              <thead>
                <tr>
                  <th scope="col">Day (UTC)</th>
                  <th scope="col" className="num">
                    Reels that day
                  </th>
                  <th scope="col" className="num">
                    Reels so far
                  </th>
                  <th scope="col">Leading topic</th>
                  <th scope="col" className="num">
                    Its share
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((point) => {
                  const leader = leadingTopic(point, history);
                  return (
                    <tr key={point.taken_at}>
                      <th scope="row">{utcDay(point.taken_at)}</th>
                      <td className="num">{point.reels_in_bucket ?? '–'}</td>
                      <td className="num">{point.n_reels}</td>
                      <td>{leader?.label ?? '–'}</td>
                      <td className="num">{leader ? `${leader.percent.toFixed(1)}%` : '–'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      }}
    </Boundary>
  );
}

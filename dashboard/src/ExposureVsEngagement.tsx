import { useQueries } from '@tanstack/react-query';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { type FingerprintKind, fetchFingerprint } from './api';
import { Boundary } from './Boundary';
import { biggestGaps, buildComparison, type CompareRow, formatGap } from './series';
import { formatPercent } from './shares';

interface TipProps {
  active?: boolean;
  payload?: readonly { payload: CompareRow }[];
}

function CompareTooltip({ active, payload }: TipProps) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return (
    <div className="tooltip">
      <div className="tooltip-title">{row.label}</div>
      <div className="tooltip-row">
        <span className="line-key" style={{ background: 'var(--exposure)' }} aria-hidden="true" />
        <strong>{formatPercent(row.exposure)}</strong>
        <span>of the exposure fingerprint</span>
      </div>
      <div className="tooltip-row">
        <span className="line-key" style={{ background: 'var(--engagement)' }} aria-hidden="true" />
        <strong>{formatPercent(row.engagement)}</strong>
        <span>of the engagement fingerprint</span>
      </div>
      <div className="tooltip-row">
        <strong>{formatGap(row.gap)}</strong>
        <span>points (engagement minus exposure)</span>
      </div>
    </div>
  );
}

/**
 * The two current fingerprints side by side, topic by topic. Each adds up to 100%, so a bar is a
 * share of that fingerprint, never an independent score. The gap is engagement minus exposure in
 * percentage points: positive means the user stayed longer on a topic than the feed's share of it.
 */
export function ExposureVsEngagement() {
  const results = useQueries({
    queries: (['exposure', 'engagement'] as const).map((kind: FingerprintKind) => ({
      queryKey: ['fingerprint', kind],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchFingerprint(kind, signal),
    })),
  });
  const [exposure, engagement] = results;
  return (
    <Boundary
      queries={results}
      what="the exposure and engagement comparison"
      empty={() =>
        (exposure?.data?.topics.length ?? 0) === 0 || (engagement?.data?.topics.length ?? 0) === 0
          ? 'Nothing to compare yet: both fingerprints are needed, and they appear once a session has been processed.'
          : null
      }
    >
      {() => {
        if (!exposure?.data || !engagement?.data) return null;
        const rows = buildComparison(exposure.data, engagement.data);
        const { over, under } = biggestGaps(rows);
        const totalExposure = Math.round(rows.reduce((s, r) => s + r.exposure, 0) * 10) / 10;
        const totalEngagement = Math.round(rows.reduce((s, r) => s + r.engagement, 0) * 10) / 10;
        return (
          <>
            <ul className="legend" aria-label="Fingerprints">
              <li>
                <span className="swatch" style={{ background: 'var(--exposure)' }} aria-hidden="true" />
                Exposure
              </li>
              <li>
                <span className="swatch" style={{ background: 'var(--engagement)' }} aria-hidden="true" />
                Engagement
              </li>
            </ul>
            <p className="meta" data-testid="gap-summary">
              {over && over.gap > 0
                ? `You stayed longer on ${over.label} than the feed's share of it (${formatGap(over.gap)} points). `
                : 'No topic got more of your time than of the feed. '}
              {under && under.gap < 0
                ? `The feed showed more ${under.label} than you stayed on (${formatGap(under.gap)} points).`
                : ''}
            </p>
            <div
              className="chart"
              style={{ height: rows.length * 44 + 40 }}
              role="img"
              aria-label="Exposure and engagement share by topic"
            >
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={rows}
                  layout="vertical"
                  barGap={2}
                  margin={{ top: 4, right: 16, bottom: 4, left: 0 }}
                >
                  <CartesianGrid stroke="var(--line)" horizontal={false} />
                  <XAxis type="number" domain={[0, 'auto']} tickFormatter={(v: number) => `${v}%`} />
                  <YAxis type="category" dataKey="label" width={130} interval={0} tick={{ fontSize: 12 }} />
                  <Tooltip content={<CompareTooltip />} cursor={{ fill: 'var(--line)', opacity: 0.4 }} />
                  <Bar dataKey="exposure" fill="var(--exposure)" radius={[0, 3, 3, 0]} isAnimationActive={false} />
                  <Bar dataKey="engagement" fill="var(--engagement)" radius={[0, 3, 3, 0]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="scroll">
              <table className="shares wide" aria-label="Exposure and engagement share by topic">
                <thead>
                  <tr>
                    <th scope="col">Topic</th>
                    <th scope="col" className="num">
                      Exposure
                    </th>
                    <th scope="col" className="num">
                      Engagement
                    </th>
                    <th scope="col" className="num">
                      Gap (points)
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.key}>
                      <th scope="row">{row.label}</th>
                      <td className="num">{formatPercent(row.exposure)}</td>
                      <td className="num">{formatPercent(row.engagement)}</td>
                      <td className="num">{formatGap(row.gap)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    <td className="num" data-testid="total-exposure">
                      {formatPercent(totalExposure)}
                    </td>
                    <td className="num" data-testid="total-engagement">
                      {formatPercent(totalEngagement)}
                    </td>
                    <td className="num">{formatGap(Math.round((totalEngagement - totalExposure) * 10) / 10)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </>
        );
      }}
    </Boundary>
  );
}

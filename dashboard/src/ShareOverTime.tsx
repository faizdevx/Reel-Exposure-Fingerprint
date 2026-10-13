import { useQueries } from '@tanstack/react-query';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { type FingerprintKind, fetchHistory } from './api';
import { Boundary } from './Boundary';
import { buildShareRows, rankTopics, type SeriesKey, seriesKeys, type ShareRow } from './series';
import { formatPercent } from './shares';

export const DAYS = 90;

interface Props {
  kind: FingerprintKind;
}

function Legend({ keys }: { keys: readonly SeriesKey[] }) {
  return (
    <ul className="legend" aria-label="Topics">
      {keys.map((k) => (
        <li key={k.key}>
          <span className="swatch" style={{ background: k.color }} aria-hidden="true" />
          {k.label}
        </li>
      ))}
    </ul>
  );
}

interface TipProps {
  active?: boolean;
  payload?: readonly { payload: Record<string, number | string> }[];
  keys: readonly SeriesKey[];
}

function ShareTooltip({ active, payload, keys }: TipProps) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  const sorted = [...keys].sort((a, b) => Number(row[b.key] ?? 0) - Number(row[a.key] ?? 0));
  return (
    <div className="tooltip">
      <div className="tooltip-title">{String(row['date'])}</div>
      {sorted.map((k) => (
        <div key={k.key} className="tooltip-row">
          <span className="line-key" style={{ background: k.color }} aria-hidden="true" />
          <strong>{formatPercent(Number(row[k.key] ?? 0))}</strong>
          <span>{k.label}</span>
        </div>
      ))}
    </div>
  );
}

function DataTable({ rows, keys, kind }: { rows: readonly ShareRow[]; keys: readonly SeriesKey[]; kind: FingerprintKind }) {
  return (
    <details className="table-view">
      <summary>Show the data as a table</summary>
      <div className="scroll">
        <table className="shares wide" aria-label={`${kind} topic share by day`}>
          <thead>
            <tr>
              <th scope="col">Day (UTC)</th>
              {keys.map((k) => (
                <th key={k.key} scope="col" className="num">
                  {k.label}
                </th>
              ))}
              <th scope="col" className="num">
                Total
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const total = keys.reduce((sum, k) => sum + (row.values[k.key] ?? 0), 0);
              return (
                <tr key={row.takenAt}>
                  <th scope="row">{row.date}</th>
                  {keys.map((k) => (
                    <td key={k.key} className="num">
                      {formatPercent(row.values[k.key] ?? 0)}
                    </td>
                  ))}
                  <td className="num" data-testid="row-total">
                    {formatPercent(Math.round(total * 10) / 10)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/**
 * Topic share over time: each day is a stacked column of shares that adds up to 100%. The topics
 * with their own colour are chosen from both fingerprints together, so a topic keeps its colour
 * when the reader switches between exposure and engagement.
 */
export function ShareOverTime({ kind }: Props) {
  const results = useQueries({
    queries: (['exposure', 'engagement'] as const).map((k) => ({
      queryKey: ['history', 'day', k],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchHistory(k, 'day', DAYS, signal),
    })),
  });
  const [exposure, engagement] = results;

  return (
    <Boundary
      queries={results}
      what="the topic share history"
      empty={() =>
        (exposure?.data?.points.length ?? 0) === 0 && (engagement?.data?.points.length ?? 0) === 0
          ? 'No history yet. A snapshot is saved for each day on which you watched Reels.'
          : null
      }
    >
      {() => {
        const histories = results.flatMap((r) => (r.data ? [r.data] : []));
        const history = (kind === 'exposure' ? exposure : engagement)?.data;
        if (!history) return null;
        const ranking = rankTopics(histories);
        const keys = seriesKeys(ranking);
        const rows = buildShareRows(history, ranking);
        if (rows.length === 0) {
          return <p className="notice empty">No {kind} history yet.</p>;
        }
        const chartData = rows.map((row) => ({ date: row.date, ...row.values }));
        return (
          <>
            <Legend keys={keys} />
            {rows.length < 2 ? (
              <p className="notice empty">
                One day so far ({rows[0]?.date}). A trend needs at least two days; the table below
                has the numbers.
              </p>
            ) : (
              <div className="chart tall" role="img" aria-label={`${kind} topic share over time`}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={chartData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                    <CartesianGrid stroke="var(--line)" vertical={false} />
                    <XAxis dataKey="date" tick={{ fontSize: 12 }} minTickGap={24} />
                    <YAxis
                      domain={[0, 100]}
                      ticks={[0, 25, 50, 75, 100]}
                      tickFormatter={(v: number) => `${v}%`}
                      width={44}
                    />
                    <Tooltip content={<ShareTooltip keys={keys} />} />
                    {keys.map((k) => (
                      <Area
                        key={k.key}
                        type="linear"
                        dataKey={k.key}
                        stackId="share"
                        stroke="var(--card)"
                        strokeWidth={2}
                        fill={k.color}
                        fillOpacity={1}
                        isAnimationActive={false}
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
            <DataTable rows={rows} keys={keys} kind={kind} />
          </>
        );
      }}
    </Boundary>
  );
}

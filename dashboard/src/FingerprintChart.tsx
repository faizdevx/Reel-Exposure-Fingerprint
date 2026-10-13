import { Bar, BarChart, Cell, ResponsiveContainer, XAxis, YAxis } from 'recharts';

import { formatPercent, type ShareRow } from './shares';

interface Props {
  /** Called with a topic row when the reader asks why it is in the fingerprint. */
  onSelect?: (row: ShareRow) => void;
  title: string;
  rows: readonly ShareRow[];
  /** CSS colour for the bars. */
  color: string;
}

const ROW_HEIGHT = 32;

/**
 * Normalised topic shares of one fingerprint: bars on a shared 0 to 100 axis plus a table with
 * the same numbers and their total. The rows add up to 100.0%, so one topic's bar is never
 * independent of the others.
 */
export function FingerprintChart({ title, rows, color, onSelect }: Props) {
  const total = rows.reduce((sum, row) => sum + row.percent, 0);
  const data = rows.map((row) => ({ ...row }));
  return (
    <div className="fingerprint">
      <div className="chart" style={{ height: rows.length * ROW_HEIGHT + 24 }} aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 0 }}>
            <XAxis type="number" domain={[0, 100]} tickFormatter={(v: number) => `${v}%`} />
            <YAxis type="category" dataKey="label" width={120} interval={0} tick={{ fontSize: 12 }} />
            <Bar dataKey="percent" isAnimationActive={false} radius={[0, 3, 3, 0]}>
              {data.map((row) => (
                <Cell key={row.key} fill={row.isRest ? 'var(--muted)' : color} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <table className="shares" aria-label={`${title} topic shares`}>
        <thead>
          <tr>
            <th scope="col">Topic</th>
            <th scope="col" className="num">
              Share
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <th scope="row">
                {onSelect && row.topicId !== null ? (
                  <button
                    type="button"
                    className="topic-button"
                    onClick={() => onSelect(row)}
                    aria-label={`Why ${row.label}? Show the Reels behind it`}
                  >
                    {row.label}
                  </button>
                ) : (
                  row.label
                )}
              </th>
              <td className="num">{formatPercent(row.percent)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Total</th>
            <td className="num" data-testid="share-total">
              {formatPercent(total)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

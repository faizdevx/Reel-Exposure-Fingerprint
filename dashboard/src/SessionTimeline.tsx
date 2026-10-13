import { useQueries } from '@tanstack/react-query';
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { fetchHistory, fetchSessions } from './api';
import { Boundary } from './Boundary';
import { formatDateTime } from './format';
import { buildTimeline, type TimelineItem } from './series';

const SESSION_LIMIT = 200;

function describe(item: TimelineItem): string {
  const length = item.minutes === null ? 'in progress' : `${item.minutes} min`;
  const reels = item.reels === null ? 'not processed yet' : `${item.reels} Reels`;
  return `${length}, ${reels}`;
}

interface TipProps {
  active?: boolean;
  payload?: readonly { payload: { item: TimelineItem } }[];
}

function TimelineTooltip({ active, payload }: TipProps) {
  const item = payload?.[0]?.payload.item;
  if (!active || !item) return null;
  return (
    <div className="tooltip">
      <div className="tooltip-title">{formatDateTime(item.startedAt)}</div>
      <div className="tooltip-row">
        <strong>{item.reels ?? '–'}</strong>
        <span>Reels counted</span>
      </div>
      <div className="tooltip-row">
        <strong>{item.minutes === null ? '–' : `${item.minutes} min`}</strong>
        <span>length</span>
      </div>
      {item.leader ? (
        <div className="tooltip-row">
          <strong>{item.leader.label}</strong>
          <span>led the exposure fingerprint afterwards</span>
        </div>
      ) : null}
    </div>
  );
}

/** Sessions in time order, with how many Reels each counted and what led the fingerprint after it. */
export function SessionTimeline() {
  const [sessions, history] = useQueries({
    queries: [
      { queryKey: ['sessions'], queryFn: ({ signal }: { signal: AbortSignal }) => fetchSessions(signal) },
      {
        queryKey: ['history', 'session', 'exposure'],
        queryFn: ({ signal }: { signal: AbortSignal }) =>
          fetchHistory('exposure', 'session', SESSION_LIMIT, signal),
      },
    ],
  });
  return (
    <Boundary
      queries={[sessions, history]}
      what="the session timeline"
      empty={() =>
        (sessions.data?.length ?? 0) === 0
          ? 'No sessions yet. Start one from the browser extension while watching Reels.'
          : null
      }
    >
      {() => {
        const items = buildTimeline(sessions.data ?? [], history.data);
        const chartData = items.map((item) => ({
          label: formatDateTime(item.startedAt),
          reels: item.reels ?? 0,
          item,
        }));
        return (
          <>
            {items.length >= 2 ? (
              <div className="chart" style={{ height: 240 }} role="img" aria-label="Reels counted per session over time">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                    <CartesianGrid stroke="var(--line)" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={28} />
                    <YAxis allowDecimals={false} width={36} />
                    <Tooltip content={<TimelineTooltip />} cursor={{ fill: 'var(--line)', opacity: 0.4 }} />
                    <Bar dataKey="reels" radius={[3, 3, 0, 0]} isAnimationActive={false}>
                      {chartData.map((d) => (
                        <Cell key={d.item.id} fill={d.item.reels === null ? 'var(--muted)' : 'var(--series-1)'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : null}
            <ol className="timeline" aria-label="Session timeline, oldest first">
              {items.map((item) => (
                <li key={item.id}>
                  <time dateTime={item.startedAt}>{formatDateTime(item.startedAt)}</time>
                  <span>{describe(item)}</span>
                  {item.leader ? (
                    <span className="meta">
                      Then led by {item.leader.label} ({item.leader.percent.toFixed(1)}% of the fingerprint)
                    </span>
                  ) : null}
                </li>
              ))}
            </ol>
          </>
        );
      }}
    </Boundary>
  );
}

import { useQuery } from '@tanstack/react-query';

import { fetchNextPrediction, type ModelFigures, type ModelPrediction, type PredictionEvaluation } from './api';
import { Boundary } from './Boundary';
import { formatPercent, roundToTotal } from './shares';

/** The only name this feature may carry: it is about what the feed shows next, not about taste. */
export const PREDICTION_TITLE = 'Predicted next observed topic';

const MODEL_ORDER = ['uniform', 'most_frequent', 'decayed_fingerprint', 'markov', 'hybrid'] as const;

const MODEL_NAMES: Record<string, string> = {
  uniform: 'Every topic equally likely',
  most_frequent: 'Your most frequent topics',
  decayed_fingerprint: 'Your fingerprint (recent Reels weigh more)',
  markov: 'What usually follows the last topic',
  hybrid: 'Fingerprint and what follows, combined',
};

const BASELINE_NAMES: Record<string, string> = {
  uniform: 'every topic equally likely',
  most_frequent: 'your most frequent topics',
  decayed_fingerprint: 'your fingerprint',
  markov: 'what usually follows the last topic',
};

const pct = (value: number | null) => (value === null ? '–' : formatPercent(value * 100));
const loss = (value: number | null) => (value === null ? '–' : value.toFixed(3));

/** Rows that add up to exactly 100.0: the top topics and everything else together. */
export function distributionRows(model: ModelPrediction): { key: string; label: string; percent: number }[] {
  const fractions = [...model.top.map((t) => t.probability), model.rest_probability];
  const percents = roundToTotal(fractions);
  const rows = model.top.map((t, i) => ({ key: t.slug, label: t.label, percent: percents[i] ?? 0 }));
  const rest = percents[model.top.length] ?? 0;
  if (model.rest_probability > 0 || rest > 0) {
    rows.push({ key: '_rest', label: 'All other topics', percent: rest });
  }
  return rows;
}

function Banner({ evaluation }: { evaluation: PredictionEvaluation }) {
  if (!evaluation.complete) {
    return (
      <p role="note" className="notice empty" data-testid="evaluation-banner">
        Evaluation not complete: {evaluation.scored_reels} of {evaluation.required_reels.toLocaleString('en-GB')}{' '}
        Reels scored. The figures below are preliminary. They do not yet show whether these predictions
        are any better than the simple ones.
      </p>
    );
  }
  const better = evaluation.hybrid_beats_required_baselines;
  return (
    <div data-testid="evaluation-banner">
      <p className="meta">
        Evaluation complete: {evaluation.scored_reels} Reels scored.{' '}
        {better
          ? "The combined model's log loss was clearly lower than both required baselines (your most frequent topics, and your fingerprint)."
          : "The combined model's log loss was not clearly lower than both required baselines (your most frequent topics, and your fingerprint)."}
      </p>
      <ul className="comparisons">
        {Object.entries(evaluation.comparisons ?? {}).map(([name, c]) => {
          const d = c.log_loss_difference;
          const range = d.low === null || d.high === null ? 'no interval' : `95% interval ${d.low.toFixed(3)} to ${d.high.toFixed(3)}`;
          const verdict =
            c.log_loss_verdict === 'hybrid_lower'
              ? 'lower'
              : c.log_loss_verdict === 'hybrid_higher'
                ? 'higher'
                : c.log_loss_verdict === 'no_interval'
                  ? 'not comparable yet'
                  : 'no clear difference';
          return (
            <li key={name}>
              Combined vs {BASELINE_NAMES[name] ?? name}: log loss difference{' '}
              {d.estimate === null ? '–' : d.estimate.toFixed(3)} ({range}): {verdict}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Scores({ evaluation }: { evaluation: PredictionEvaluation }) {
  const row = (model: string, figures: ModelFigures, all: ModelFigures) => (
    <tr key={model}>
      <th scope="row">{MODEL_NAMES[model] ?? model}</th>
      <td className="num">{pct(figures.top1)}</td>
      <td className="num">{pct(figures.top3)}</td>
      <td className="num">{loss(figures.log_loss)}</td>
      <td className="num">{pct(all.top1)}</td>
      <td className="num">{all.n}</td>
    </tr>
  );
  return (
    <div className="scroll">
      <table className="shares wide" aria-label="How earlier predictions scored">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col" className="num">
              Top-1 hits, last {evaluation.rolling_window}
            </th>
            <th scope="col" className="num">
              Top-3 hits, last {evaluation.rolling_window}
            </th>
            <th scope="col" className="num">
              Log loss, last {evaluation.rolling_window}
            </th>
            <th scope="col" className="num">
              Top-1 hits, all
            </th>
            <th scope="col" className="num">
              Reels scored
            </th>
          </tr>
        </thead>
        <tbody>
          {MODEL_ORDER.map((m) => {
            const figures = evaluation.models[m];
            return figures ? row(m, figures.rolling, figures.all_time) : null;
          })}
        </tbody>
      </table>
    </div>
  );
}

export function PredictionPanel() {
  const query = useQuery({
    queryKey: ['predictions', 'next'],
    queryFn: ({ signal }) => fetchNextPrediction(signal),
  });
  return (
    <section className="card block" aria-labelledby="prediction-heading">
      <h2 id="prediction-heading">{PREDICTION_TITLE}</h2>
      <Boundary
        queries={[query]}
        what="the prediction"
        empty={() =>
          query.data && !query.data.available
            ? 'No predictions yet. They appear once Reels from your sessions have been processed.'
            : null
        }
      >
        {() => {
          const data = query.data;
          if (!data?.hybrid || !data.evaluation) return null;
          const rows = distributionRows(data.hybrid);
          const total = Math.round(rows.reduce((s, r) => s + r.percent, 0) * 10) / 10;
          return (
            <>
              <p className="blurb">{data.note}</p>
              <p className="meta">
                From {data.based_on_reels} Reels
                {data.last_topic ? `. The last observed topic was ${data.last_topic.label}` : ''}.
              </p>
              <table className="shares" aria-label="Predicted next observed topic, combined model">
                <thead>
                  <tr>
                    <th scope="col">Topic</th>
                    <th scope="col" className="num">
                      Probability
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.key}>
                      <th scope="row">{r.label}</th>
                      <td className="num">{formatPercent(r.percent)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    <td className="num" data-testid="prediction-total">
                      {formatPercent(total)}
                    </td>
                  </tr>
                </tfoot>
              </table>
              <p className="meta" data-testid="prediction-basis">
                {data.backoff
                  ? data.last_topic === null || data.row_observations === null
                    ? 'There is no earlier Reel to go on yet, so this is your fingerprint.'
                    : `After ${data.last_topic.label} you have ${data.row_observations} recorded follow-ups, fewer than the ${data.min_row_observations} needed, so your fingerprint is used instead.`
                  : `Mixes your fingerprint (weight ${data.hybrid_lambda?.toFixed(1)}) with what followed ${data.last_topic?.label} in your ${data.row_observations} earlier follow-ups.`}
              </p>
              <h3>How earlier predictions scored</h3>
              <Banner evaluation={data.evaluation} />
              <Scores evaluation={data.evaluation} />
              <p className="meta">
                Top-3 is a hit when the observed topic was among the three most likely. Log loss is lower
                for better-calibrated predictions. Each prediction was made before its Reel was counted.
              </p>
            </>
          );
        }}
      </Boundary>
    </section>
  );
}

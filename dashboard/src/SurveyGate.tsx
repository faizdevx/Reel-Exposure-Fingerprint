import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { type FormEvent, type ReactNode, useState } from 'react';

import { fetchSelfReport, skipSelfReport, submitSelfReport, type SurveyState } from './api';
import { Boundary } from './Boundary';

const SCALE = [1, 2, 3, 4, 5] as const;

/** Rating 1 to 5 for every topic, before the first look at a fingerprint (spec §10). */
function SurveyForm({ survey }: { survey: SurveyState }) {
  const queryClient = useQueryClient();
  const [ratings, setRatings] = useState<Record<string, number>>({});
  const done = (state: SurveyState) => queryClient.setQueryData(['self-report'], state);
  const submit = useMutation({ mutationFn: () => submitSelfReport(ratings), onSuccess: done });
  const skip = useMutation({ mutationFn: skipSelfReport, onSuccess: done });
  const answered = survey.topics.filter((t) => ratings[String(t.topic_id)] !== undefined).length;
  const complete = answered === survey.topics.length;
  const busy = submit.isPending || skip.isPending;

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (complete && !busy) submit.mutate();
  };

  return (
    <section className="card block" aria-labelledby="survey-heading">
      <h1 id="survey-heading">Before you see your fingerprints</h1>
      <p className="lead">
        Rate how interested you are in each topic, from 1 (not at all) to 5 (very). We ask first so your
        answers are not shaped by what the dashboard shows. They are used only to check, for research,
        how well the fingerprints match what you say. You can answer once; you can also skip.
      </p>
      <form onSubmit={onSubmit}>
        {survey.topics.map((topic) => (
          <fieldset key={topic.topic_id} className="rating">
            <legend>{topic.label}</legend>
            {SCALE.map((value) => (
              <label key={value}>
                <input
                  type="radio"
                  name={`topic-${topic.topic_id}`}
                  value={value}
                  checked={ratings[String(topic.topic_id)] === value}
                  onChange={() => setRatings((r) => ({ ...r, [String(topic.topic_id)]: value }))}
                />
                <span>{value}</span>
              </label>
            ))}
          </fieldset>
        ))}
        <p className="meta" aria-live="polite">
          {answered} of {survey.topics.length} topics rated.
        </p>
        {submit.isError || skip.isError ? (
          <p role="alert" className="notice error">
            Could not save your answers. Nothing was changed; please try again.
          </p>
        ) : null}
        <div className="confirm">
          <button type="submit" disabled={!complete || busy}>
            Save my ratings
          </button>
          <button type="button" className="link" disabled={busy} onClick={() => skip.mutate()}>
            Skip this
          </button>
        </div>
      </form>
    </section>
  );
}

/** Shows the survey instead of its children until the user has answered or skipped it. */
export function SurveyGate({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ['self-report'],
    queryFn: ({ signal }) => fetchSelfReport(signal),
  });
  return (
    <Boundary queries={[query]} what="the survey" empty={() => null}>
      {() => (query.data?.status === 'pending' ? <SurveyForm survey={query.data} /> : children)}
    </Boundary>
  );
}

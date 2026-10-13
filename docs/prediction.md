# Next-topic prediction

**Status: built and tested. No predictive success is claimed.** Spec §10 asks for at least 1,500 scored Reels per user before any claim, and for the hybrid to beat the most-frequent and decayed-fingerprint baselines, "or say so". No user has that much data yet, so every figure the dashboard shows is labelled preliminary, and the system states no comparison until the minimum is reached.

## What is predicted

The task is: **before** a Reel is counted, say which topic the next observed Reel will have. A Reel's observed topic is the top topic of its fused distribution (ties go to the lower topic id; `other` is a topic when the pipeline version uses it). Reels without a topic distribution are not part of the sequence, as in the fingerprints.

The feature is named **"Predicted next observed topic"** everywhere. It predicts what the feed shows next. It is not a prediction of what the user likes or prefers, the API note says so, and nothing may be worded otherwise.

## The five models

`P` is over the universe `U` of topics (the taxonomy, plus `other` if the assignment version has ever assigned it).

| Model | Distribution |
|---|---|
| `uniform` | `1 / |U|` for every topic |
| `most_frequent` | add-one smoothed counts of the user's observed topics so far: `(c_k + 1) / (N + |U|)` |
| `decayed_fingerprint` | the topic shares of the user's exposure fingerprint (`W_k / sum W`, recent Reels weigh more) |
| `markov` | the row of the last topic: `(n(last, k) + 1) / (N_last + |U|)`. **Backoff:** when `N_last < 20`, or there is no last topic, it is the fingerprint distribution |
| `hybrid` | `lambda * P_fingerprint + (1 - lambda) * P_transition`, with the same backoff to the fingerprint |

`lambda` is chosen from past data only (walk-forward): the value of the grid `0.1, 0.3, 0.5, 0.7, 0.9` with the lowest cumulative log loss on the Reels already seen, and the default 0.5 until 100 Reels have been seen. It never looks at the Reel it is predicting. Ties go to the value nearest the default, then the smaller.

## Stored before the next event

Every Reel goes through two steps in this order (`app/prediction/engine.py`): its prediction is made from the state before it, and only then does it update the state (transition counts, topic counts, the exposure fingerprint, the log loss of each `lambda`). Tests show this three ways: the stored prediction of event *i* equals the prediction from the Reels before *i* alone; changing Reel *i* never changes its own prediction, only later ones; and a rebuild adds predictions for new Reels without rewriting earlier ones.

Storage: `predictions` has one row per Reel and model (`made_before_event`, `model`, `distribution`, `actual_topic`, `hit_top1`, `hit_top3`, `pipeline_version`); `topic_transitions` holds the user's transition counts; the new table `prediction_state` (migration 0004) holds the prediction for the Reel that has not happened yet (it cannot live in `predictions`, whose rows must name an existing event) and the evaluation summary, so reading them is one row and never a scan of the history.

Rows come from `rebuild_predictions` (`python -m app.manage rebuild-fingerprints --assignment-version V`, which now also rebuilds predictions; `--skip-predictions` to leave them). There is still no job loop that does it on its own. A rebuild replaces that user's rows of the same version, so repeating it changes nothing.

## Evaluation (spec §10)

Prequential: every prediction was made before its Reel. Scored by top-1 (the model's most likely topic was the observed one), top-3, and log loss `-ln p(observed)` with `p` floored at 1e-12. All-time and rolling (last 100) figures per model are stored.

Until 1,500 Reels are scored, `evaluation.complete` is false, the status says "not complete: N of 1500 Reels scored", and there is no comparison and no verdict (`comparisons: null`, `hybrid_beats_required_baselines: null`). At or above the minimum, the hybrid's per-Reel log loss and top-1 differences against each other model are given with 95% cluster-bootstrap intervals over sessions, and each is called `hybrid_lower`, `no_clear_difference` or `hybrid_higher` from the interval alone. `hybrid_beats_required_baselines` is true only when the hybrid's log loss is clearly lower than both the most-frequent and the decayed-fingerprint baselines. If it is false the dashboard says the baselines were not clearly beaten.

## Known limits

- Predictions use the exposure fingerprint and the sequence of observed topics; nothing about the user's engagement.
- The universe of topics is a property of the assignment version, not of the moment, so adding a topic means a new version and a rebuild.
- `lambda`'s grid and default, the transition threshold (20) and the add-one smoothing are the spec's starting values, not tuned. The minimum for a conclusion is 1,500 Reels; data should accumulate from now on.
- Predictions are rebuilt, not updated incrementally, until the worker has a job loop.
- Session deletion rebuilds predictions from the remaining Reels (`docs/api.md`), from the settings the state recorded.
- Numbers shown for a few hundred Reels say nothing about how well the predictions work; random Reel sequences give the same kind of figures.

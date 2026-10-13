"""Next-topic prediction (spec §5.2, F12, §10).

The task: before each Reel is applied to the model state, say which topic the next observed Reel
will have. A Reel's observed topic is the top topic of its fused distribution (`other` when the
Reel was assigned to it). This predicts what the feed will show next; it says nothing about what
the user likes or prefers, and nothing here or on the dashboard may be worded that way. The label
is `PREDICTION_LABEL`.

Five models, all in `engine.py`:

1. `uniform`: every topic equally likely.
2. `most_frequent`: the user's own topic counts so far, add-one smoothed.
3. `decayed_fingerprint`: the share of each topic in the exposure fingerprint, which weights recent
   Reels more.
4. `markov`: the add-one smoothed probability of the next topic given the last one, learned from
   this user's consecutive Reels; it falls back to the fingerprint while the row of the last topic
   has fewer than 20 observations.
5. `hybrid`: `lambda * P_fingerprint + (1 - lambda) * P_transition`, with the same fallback.
   `lambda` is chosen from past data only (walk-forward): the grid value with the lowest
   cumulative log loss on the earlier Reels, and a fixed default until there are enough of them.

The evaluation is prequential: every prediction is made from the state before its Reel, written
down, and only then does the Reel update the state. Nothing may claim predictive success before the
minimum number of scored Reels (spec §10) and an interval-based comparison with the baselines.

Layout: `config.py` (settings and the version), `engine.py` (pure maths and the replay),
`evaluation.py` (metrics and the comparison), `store.py` (database), the endpoint in
`app/routers/predictions.py`.
"""

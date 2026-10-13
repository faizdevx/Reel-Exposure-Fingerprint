"""Evaluation of topic assignment: text only, visual only and fused, on a hand-labelled set.

What it answers (spec §10): are the labels right, and does fusion help? It also sets the settings
that spec §5.2 leaves open: the fusion weights, the two softmax temperatures and the `other`
threshold. Those settings are chosen from data here and nowhere else.

Layout:

- `labels.py`: the labelled-set file (JSON Lines) and its checks.
- `cosines.py`: runs the real pipelines once per Reel and stores the cosine of the Reel against
  every topic, per modality. Probabilities for any temperature are computed from these, so a
  sweep never runs a model again.
- `metrics.py`: accuracy, log loss, `other` handling, cluster bootstrap intervals.
- `tuning.py`: a group-aware split, the grid, the selection rule, and the guard that refuses to
  recommend anything from too little data.
- `__main__.py`: `python -m app.evaluation embed|evaluate`.

Rules that keep the result honest:

1. Reels are split into a development part and a held-out test part by group (the same person or
   source never sits on both sides). Settings are chosen on the development part only. The test
   part is scored once, for the chosen settings and for the defaults, and never used to choose.
2. The best grid point is not taken at face value. Among all points within one standard error of
   the best log loss, the one nearest the spec defaults wins, so noise does not move the settings.
3. Below the minimum data (`MIN_LABELLED`, `MIN_GROUPS`, `MIN_BOTH`, `MIN_TEST`) the report is
   still written, with metrics and intervals, but its recommendation is `insufficient_data` and no
   setting is proposed.
4. Everything is seeded and every run records its inputs (label file hash, versions, seed) and its
   outputs, so a result can be repeated.

Intervals are cluster bootstrap intervals over groups: Reels from one source are not independent,
and a plain bootstrap over Reels would make small samples look more certain than they are.
"""

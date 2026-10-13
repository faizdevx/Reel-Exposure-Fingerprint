# Late fusion, the `other` class, and how the settings get chosen

**Status: implemented and tested; no setting has been tuned, because the hand-labelled set does not exist yet.** `docs/final-spec.md` §10 asks for 200 hand-labelled Reels and M3 says to start them; none are in the repository, and the M0 probe that would supply real frames has not been run. So the fusion weights stay at the spec's 0.5 / 0.5, the temperatures stay at their provisional values, and the `other` threshold is **unset** (no Reel is ever assigned to `other`). The evaluation scripts are ready and refuse to recommend anything from too little data.

## Fusion and `other` (what the pipeline does)

Code: `server/app/worker/fusion.py`, `reel_processor.py`, `vision_config.py`.

- **Fusion.** For each modality that exists, a topic distribution: `p_text` (e5, softmax of cosine over `tau_text`) and `p_visual` (SigLIP, over `tau_visual`). `p(k) = sum_m w_m * p_m(k) / sum_m w_m` over the modalities present, so the weights are renormalised when one is missing. Start `w_text = w_visual = 0.5`. Raw embeddings are never combined: they have different sizes (384 and 768) and live in different spaces; `fuse` accepts probability maps only.
- **`other`.** If `max_k p_fused(k)` is below `FusionConfig.other_threshold`, the Reel is assigned to `other`. The threshold is part of the config and of the pipeline version, and **has no default**: `None` means no Reel is ever `other`. A Reel with no usable modality is not `other`; it is unprocessed and stores nothing.
- **What is stored** (`topic_assignments`, one row per topic): `p_text`, `p_visual` and `p_fused`, plus `pipeline_version`. For a Reel assigned to `other`, an extra row for topic 0 (`other`, label "Other or unclear", created in the `topics` table) holds `p_fused = 1.0`, and every taxonomy topic's `p_fused` is 0.0, so the column still sums to 1 per Reel. The per-modality columns keep what each modality actually thought, as evidence. The distribution before the `other` decision is not stored; it can be recomputed from `p_text`, `p_visual` and the weights in the version.
- **Version.** A text-only pipeline with no threshold keeps its old `pv1-` version. Anything with a visual model or a threshold is `pv2-`, a hash over both models (id, revision, licence), the taxonomy, both temperatures, the weights and the threshold. A text-only pipeline with a threshold is `pv2-` too, because the threshold changes what is stored.
- **Downstream.** The fingerprint loader accepts the 28 topics plus topic 0, so an `other` Reel adds weight to `other` in both fingerprints and the dashboard shows it under its label.

## Evaluation (what the scripts do)

Code: `server/app/evaluation/`. Run from `server/`:

```
# 1. Run the real models once per labelled Reel and keep the cosines (slow, needs the ml group):
python -m app.evaluation embed --labels ../labels/labels.jsonl --out ../labels/cosines.npz \
    --visual-model siglip2-base          # or siglip-multilingual; omit for text only

# 2. Compare text only, visual only, fused; tune; write a report (fast, no model):
python -m app.evaluation evaluate --cache ../labels/cosines.npz \
    --labels ../labels/labels.jsonl --out ../labels/report.json --seed 0
```

Because the cache stores cosines, not probabilities, every temperature and weight in the sweep is computed without running a model again.

**The labelled-set file** (`labels.jsonl`, one object per line; format and checks in `app/evaluation/labels.py`): `id`, `label` (a taxonomy slug, or `other`), `group` (the person or source the Reel came from), and optionally `caption`, `hashtags`, `ocr_text`, `frames` (up to 5 paths under the labels folder) and `annotator`. Missing text or frames stay missing. Errors name the line, never the content. A labelled set made from real Reels is data about real people and must stay out of git.

**What is compared.**

| View | Reels | Use |
|---|---|---|
| `paired` | only Reels that have both text and frames | the fair comparison of text only, visual only and fused, plus paired differences of log loss with intervals |
| `all_available` | every Reel each mode can score | what the product does; the three modes cover different Reels, so do not compare them with each other |

Metrics: top-1, top-3 and log loss over topic-labelled Reels (a Reel labelled `other` has no topic to be right about), and, for the threshold, accuracy with `other` as a class and the share assigned to a topic. Intervals are cluster bootstrap intervals over groups (1000 resamples, seeded): Reels from one source are not independent, and a plain bootstrap over Reels would make a small sample look more certain than it is. With fewer than two groups there is no interval, not a made-up one.

**How settings are chosen (and not over-fitted).**

1. Reels are split into development and held-out parts by group, with a seed; the same source is never on both sides.
2. On the development part only, a grid is scored by fused log loss on Reels with both modalities: weight of text in {0.2, 0.35, 0.5, 0.65, 0.8}, `tau_text` and `tau_visual` each in {0.005, 0.01, 0.02, 0.05} (80 points).
3. The best point is not taken as it is. Every point within one standard error of it counts as equally good, and the one nearest the defaults is chosen.
4. The held-out part is scored once, for the chosen setting and for the defaults. The chosen setting is recommended only if its held-out log loss is better than the defaults' with an interval that excludes zero. Otherwise the defaults stay and the report says so.
5. The `other` threshold is chosen the same way (accuracy with `other` as a class, smallest threshold within one standard error of the best, `None` preferred), and kept only if it beats "never `other`" on the held-out Reels with an interval above zero. It needs at least 20 Reels labelled `other`.
6. **Minimum data**, below which the report has metrics and intervals but no recommendation and nothing is tuned: 200 labelled Reels, 10 groups, 100 Reels with text and frames, 40 such Reels on each side of the split.

Log loss is the selection criterion because it is a proper scoring rule and rewards calibrated probabilities, which the `other` threshold depends on. One consequence, found by a test: on data with no signal at all, a flatter temperature is correctly judged better (honest uncertainty scores better than false confidence), so temperatures may move toward flat on weak data. The weights and the threshold do not move on noise (tested over six seeds).

## Dry run (not the labelled set)

To prove the scripts on real models, a throwaway labelled set was built from material already in the repository: the 36 hand-written captions of `tests/text_samples.py` (text only) and the 16 labelled synthetic photos of M0 (frames only), 51 Reels in 11 groups, with SigLIP 2 base. It is not the hand-labelled set: the captions were written by the same author as the taxonomy, so they are easy, and the photos are 8 pictures shown twice. No Reel has both text and frames, so no pairing, weighting or tuning was possible.

Result: `insufficient_data` (labelled Reels 51 of 200, Reels with text and frames 0 of 100), nothing tuned, as it should be. The defaults, scored per mode on the Reels each can see: text 35 Reels, top-1 100% (interval 100 to 100, a sign of how easy the captions are); visual 16 Reels, top-1 44%, top-3 63%; "fused" 51 Reels, top-1 82%. The fused figure is a mix of the other two populations and says nothing about whether fusion helps. Do not read anything into these numbers.

## Needed from you

1. **The labelled set.** Hand-label at least 200 Reels from real frames: about 100 with both caption text and frames, spread over at least 10 sources, with at least 20 that fit no topic (`other`). Label before looking at model output. This also needs the M0 probe to get real frames.
2. Run `embed` and `evaluate`, then record the report here and set the confirmed values in `vision_config.py` / `config.py` (a new `pipeline_version`).
3. Decide ADR-010 (what happens to a Reel stored before its frames arrive), since it affects which Reels get both modalities.

## Tests

| File | Covers |
|---|---|
| `tests/test_fusion.py` | the `other` decision and distribution, threshold validation |
| `tests/test_vision_config.py` | the threshold in the version, text-only plus threshold is `pv2-` |
| `tests/test_reel_processor.py` (pgvector) | `other` stored and cached, evidence kept, stage skipped without a threshold, new version beside the old |
| `tests/test_fingerprint_store.py` (pgvector) | an `other` Reel counts in the fingerprint |
| `tests/test_evaluation.py` (57) | vectorised softmax and fusion agree with the pipeline code, metrics by hand, bootstrap, group split, each minimum, reproducibility, pure noise does not move weights or threshold, a better modality pulls the weight, held-out Reels play no part in choosing, threshold found when unsure Reels are labelled `other`, labelled-set file checks, cosine cache, CLI |

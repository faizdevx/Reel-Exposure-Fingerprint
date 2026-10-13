# Visual embedding and scoring: design and evaluation

**Status: implemented and tested with fake and real encoders; the model choice is open and the accuracy numbers are a dry run.** No SigLIP model has been selected: the M0 feasibility report does not exist, and the M0 browser probe has not been run. Both candidates are implemented, selected by a setting with no default, and benchmarked below on synthetic and stock-photo frames. Nothing here decides between them.

## What was built

Code: `server/app/worker/` (`vision_config.py`, `visual_embedder.py`, `visual_topics.py`, `visual_pipeline.py`, `fusion.py`, `reel_processor.py`).

```
JPEG frames (at most 5) -> embed_frames (SigLIP image tower, one frame at a time)
                        -> pool_frames (mean of unit vectors, scaled to unit length)
taxonomy descriptions   -> load_visual_taxonomy_embeddings (SigLIP text tower, cached on disk)
                        -> score_visual: softmax(cos / tau_visual) over the same 28 topics
text pipeline (e5)      -> p_text                                   (unchanged)
fuse                    -> p_fused = weighted mean of p_text and p_visual, renormalised over present modalities
store_features / store_assignments -> content_features, topic_assignments
```

Rules that follow from the task and spec §5.2:

- **Embeddings are never combined.** e5 vectors (384 numbers) and SigLIP vectors (768) live in different spaces. Topic scoring compares an image vector with text-tower vectors of the same SigLIP model only. Modalities meet only as probabilities (`fusion.py` takes topic-to-probability maps and nothing else; a test checks its signature and that it imports no numpy). A test shows that scoring an image vector against e5 topic vectors fails with an error instead of returning a number.
- **Probabilities are stored separately.** `topic_assignments` rows hold `p_text`, `p_visual` and `p_fused`. A Reel with no frames has `p_visual` NULL and `p_fused` equal to the text distribution; a Reel with frames but no usable text has `p_text` NULL and `p_fused` equal to the visual one (spec F9); a Reel with neither stores nothing.
- **Frames:** distinct frames sorted by the SHA-256 of their bytes, at most 5, so the pooled vector does not depend on upload order. A frame that cannot be decoded is dropped and counted; only if none works does the stage fail. Preprocessing is the model's own image processor.
- **Taxonomy in the image space:** each topic is the mean of the unit text-tower vectors of its descriptions (English ones inside "this is a photo of {}.", Hindi ones as they are). Cached as `visual-topic-embeddings-<key>.npz`, keyed by model id, revision, template, languages, thread count and the taxonomy content hash; checked on load, rebuilt if damaged, never stale.
- **Independent failure:** each stage reports `started_at`, `finished_at`, `duration_ms`, `status`, `error`. A failed visual stage leaves the text result intact and the reverse, and a failed fusion leaves both modalities computed but stores no assignment.

## Caching and `pipeline_version`

Rows are keyed by `(reel_shortcode, pipeline_version)`. A second run for the same Reel and version reads the stored assignments and runs no model (tested: no e5 call, no image call, no text-tower call). A Reel whose features are stored but whose assignments are missing is re-scored from the stored embeddings, again without running a model.

With no visual model configured the version is exactly the text pipeline's (`pv1-...`). With one it is `pv2-...`, a hash of: the text configuration (e5 model id and revision, normaliser, taxonomy content hash, tau_text), the visual model **id, commit revision, licence and date verified**, dimension, thread count, prompt template, text length, which description languages, the frame rules, pooling, `tau_visual`, and the fusion weights. Changing any one of them changes the version, and rows under the old version are kept (tested for nine parameters, for both models, and for a reworded taxonomy).

Models verified on Hugging Face on 2026-10-08 (API): both `apache-2.0`, not gated, revision equal to the one the M0 benchmark ran.

| Key | Model id | Revision | Licence |
|---|---|---|---|
| `siglip2-base` | `google/siglip2-base-patch16-224` | `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2` | apache-2.0 |
| `siglip-multilingual` | `google/siglip-base-patch16-256-multilingual` | `8952a4eafcde3cb7ab46b1dd629b33f8784ca9c6` | apache-2.0 |

## Decisions and known limits

1. **Model choice (open).** `REF_VISUAL_MODEL` has no default. Unset means text only. Choosing needs a labelled set of real frames (the M0 probe plus the 200-Reel label audit).
2. **First writer wins.** A Reel stored without its visual part (frames not uploaded yet, or the visual stage failed) stays text-only under that version; frames that arrive later are not added. The "upgrade once" behaviour belongs to ADR-010, which is Proposed and not applied.
3. **`tau_visual` is provisional (0.02).** On the dry run, tau changes confidence but not rank (see below). With the text temperature at 0.01 and the visual at 0.02, the visual distribution is flatter, so text dominates the fused result. The M4 label audit must tune both temperatures and the fusion weights together.
4. **The `other` threshold** (spec §5.2) is implemented and unset; it is chosen from the labelled set by `python -m app.evaluation` (`docs/fusion-evaluation.md`).
5. **Hindi descriptions** are used with the image model's text tower without having been tested for quality. `description_languages=("en",)` is available and is part of the version.
6. **Not built:** frame ordering by sharpness, OCR, the job loop that feeds frames to this code, and any fingerprint change.

## Benchmark (dry run on synthetic frames, not an evaluation)

Source: `spikes/ml-bench/results/visual-synthetic-4t-20261008-215224.json`, run with `spikes/ml-bench/visual_bench.py` through the shipped pipeline code. Intel Family 6 Model 141, 12 logical CPUs, 15.7 GB, no GPU, 4 torch threads, torch 2.14.1+cpu, transformers 5.19.0. 30 frames, of which 16 carry a label.

| Measure | `siglip2-base` | `siglip-multilingual` |
|---|---|---|
| Latency per frame, p50 / p95 | 156 / 198 ms | 202 / 227 ms |
| Latency per 5-frame Reel, p50 / p95 | 844 / 918 ms | 1051 / 1065 ms |
| Stage means per Reel: embed / pool / score | 826 / 18 / 3 ms | 1036 / under 0.01 / under 0.01 ms |
| Taxonomy embeddings, cold build / warm cache | 9.8 s / 19 ms | 9.6 s / 12 ms |
| Model load plus first frame | 43.7 s (cold disk) | 13.5 s |
| CPU seconds per wall second | 3.95 | 3.95 |
| Memory (resident / peak, whole process) | 2053 / 2089 MB | 1857 / 2089 MB |
| Repeat run and reversed frame order, max difference | 0.0 / 0.0 | 0.0 / 0.0 |
| Zero-shot top-1 / top-3 over 28 topics (n=16) | 9 / 13 | 14 / 14 |

Notes on reading it:

- **Throughput.** The visual stage costs about 0.85 s (SigLIP 2) or 1.05 s (multilingual) per Reel of 5 frames, using about 4 cores. With OCR on 2 frames (about 1 s, `docs/ocr-evaluation.md`) and text (about 20 ms), a Reel takes roughly 2 to 2.2 s when the stages run one after another, inside the 3 s target (N3) with little room. The stages each saturate the CPU, so they do not overlap. The pool and score stages cost nothing measurable; the 18 ms for SigLIP 2 is first-call noise.
- **Memory.** Both models ran in one process, so the second model's peak includes what the first left behind. Use the SigLIP 2 line (about 2.1 GB, including torch and the text tower) as the cleaner figure for one model.
- **Accuracy is a smoke test.** The 16 labelled frames are 8 stock photos each shown twice with an English and a Hindi overlay, so the effective sample is about 8. The labels were mapped by hand from M0's labels to taxonomy topics. The multilingual model scored higher (14 against 9 top-1), which is the same direction as in M0 (88% against 69%), but eight photos cannot support a choice, and the gap may be the overlay text rather than the picture.
- **Temperature.** Rank is the same at every tau; only confidence moves. Mean top-topic probability by tau 0.01 / 0.02 / 0.05 / 0.1: SigLIP 2 0.42 / 0.18 / 0.08 / 0.05; multilingual 0.68 / 0.38 / 0.11 / 0.07 (uniform over 28 topics is 0.036).
- **Determinism.** Same frames give bit-identical embeddings on this machine, in any order. Not guaranteed across CPU models or torch versions (differences of about 1e-7 are expected).

## Tests

| File | Covers | Needs |
|---|---|---|
| `tests/test_vision_config.py` (18) | registry (pinned revisions, licences), `pipeline_version` content and sensitivity | nothing |
| `tests/test_visual_pipeline.py` (34) | taxonomy embeddings and cache, frame choice and pooling, scoring, stage reports, failures, cached embedding | nothing (fake encoder) |
| `tests/test_fusion.py` (17) | late fusion maths and refusals | nothing |
| `tests/test_reel_processor.py` (19) | storage of the three probabilities, cache by shortcode and version, missing and failing modalities | pgvector |
| `tests/test_visual_model.py` (14) | both real SigLIP models: shapes, units, determinism, order independence, text tower | `REF_TEST_MODEL=1` and the model files |

## Next

1. Choose the model from real frames: run `visual_bench.py --source real` on the labelled M0 frames, then the 200-Reel audit.
2. Tune `tau_text`, `tau_visual` and the fusion weights together, and set the `other` threshold.
3. Decide ADR-010 (what happens to a Reel stored before its frames arrive).

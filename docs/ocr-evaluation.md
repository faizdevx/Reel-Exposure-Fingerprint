# OCR evaluation

**Status: the benchmark is not complete.** It requires 50 representative real frames, and none exist yet. The M0 probe has not been run (`docs/m0-feasibility-report.md` does not exist), so there are no frames captured from Instagram. This document therefore holds:

1. a **dry run** on the 30 synthetic frames from M0, to prove the harness works and to give early signals, and
2. the **protocol** to produce the real result.

Nothing below is a decision. The OCR architecture is unchanged: no OCR stage exists in the worker yet, and none will be built until the real benchmark is in this file.

## What is being decided

| Question | Options |
|---|---|
| Engine | RapidOCR (ONNX, preferred) or EasyOCR (fallback if Devanagari quality is inadequate) |
| RapidOCR recogniser | default (Chinese and English) or Devanagari (PP-OCRv5 mobile) |
| Confidence threshold | the cut-off under which OCR lines are dropped |

Fixed constraints from the task and spec (§5.1, §12): OCR at most 2 frames per Reel (the sharpest two), keep only text above a confidence threshold, OCR failure must not fail the Reel (independent stage), store the OCR confidence, and record the pipeline version. Throughput target N3 is at least 1 Reel per 3 s on 4 vCPU without a GPU.

## Harness

`spikes/ml-bench/ocr_bench.py` (throwaway spike code, not imported by shipped code). Each engine runs in its own process, so load time and peak memory are not polluted by the others. It records, per engine:

- latency per frame after one warm-up call (mean, p50, p95, max);
- CPU: CPU seconds used per wall second over the timed loop, and peak working set;
- accuracy per language: the expected text is compared with the best matching part of the OCR output (edit distance divided by the expected length, "CER"); a frame is "recovered" at CER ≤ 0.1;
- confidence: mean line score per language, and for thresholds 0.0 to 0.9 the median CER, frames recovered, characters kept, and frames where text appeared although none was expected (false text);
- failures: a frame that raises is counted and does not stop the run.

The output file records `frameSource` and refuses to call itself a benchmark unless `source=real` and at least 50 frames (`countsAsBenchmark`).

## Dry run: synthetic frames (not the benchmark)

Source: `spikes/ml-bench/results/ocr-synthetic-4t-20261008-213318.json`. 30 synthetic frames (stock photos with overlaid English, Hindi and mixed text, plus text cards), 4 threads, Intel Family 6 Model 141 CPU with 12 logical CPUs and 15.7 GB RAM, no GPU. EasyOCR 1.7.2 (Hindi and English), RapidOCR as in the M0 bench.

| Measure | RapidOCR default | RapidOCR Devanagari | EasyOCR hi+en |
|---|---|---|---|
| Latency p50 / p95 per frame | 542 / 618 ms | 520 / 573 ms | 2929 / 3610 ms |
| Estimated OCR time for 2 frames | about 1.1 s | about 1.0 s | about 5.9 s |
| CPU seconds per wall second (4 threads set) | 4.7 | 4.9 | 3.9 |
| Peak memory | 402 MB | 390 MB | 1225 MB |
| Load time | 3.4 s | 1.3 s | 9.8 s |
| English recovered (n=14) | 14 | 14 | 9 |
| Hindi recovered (n=14) | 0 (6 with no output) | 5 (3 with no output) | 13 |
| Mixed recovered (n=2) | 0 | 1 | 2 |
| Mean line score, English / Hindi | 0.996 / 0.787 | 0.980 / 0.914 | 0.697 / 0.575 |
| Frames that raised | 0 | 0 | 0 |

### Observations

- **Language quality.** The default RapidOCR recogniser cannot read Devanagari (empty output or unrelated characters). The Devanagari recogniser reads some Hindi lines exactly and fails on others. EasyOCR read 13 of 14 Hindi frames and both mixed frames, but only 9 of 14 English frames, against 14 of 14 for both RapidOCR configurations. On this data no single engine is best on every language.
- **Latency.** EasyOCR is about 5.6 times slower per frame. Two frames per Reel would cost about 6 s of OCR alone, twice the whole N3 budget of 3 s per Reel, before SigLIP and e5. RapidOCR at about 1 s for two frames fits, but at 4 to 5 cores busy the OCR stage would use all 4 vCPU while it runs.
- **CPU.** Both RapidOCR runs used more CPU seconds per wall second than the 4 threads configured (4.7 and 4.9), so ONNX Runtime and OpenMP helper threads are running as well. Plan for OCR to saturate the machine; do not assume it shares cores with another stage.
- **Memory.** EasyOCR peaked at about 1.2 GB against 0.4 GB for RapidOCR. Relevant to the 8 GB hosted option, and to running the worker beside PostgreSQL.
- **Confidence is not comparable between engines.** EasyOCR scores correct text low: its recovered frames fall from 24 at no threshold to 16 at 0.5, 10 at 0.6 and 7 at 0.7. RapidOCR scores are high (mean 0.98 to 0.996 on English) and its recovery does not change from 0 to 0.9. A single threshold for both is wrong, and any threshold must be chosen per engine from real frames. The mean line score of RapidOCR on the frames it got wrong is also high (in the M0 run, 0.99 on a Hindi frame where only an English watermark was read), so a high score does not prove correctness.
- **No false-text test was possible.** All 30 synthetic frames contain text, so the harness's false-text counts are 0 by construction. Real Reels contain many frames with no overlaid text (faces, scenery), where OCR may read signs, logos or noise. This is the main thing the real benchmark must measure.
- **Pooled medians mislead.** The "median CER over all frames" in the threshold table mixes English and Hindi; for RapidOCR Devanagari it is 0.0 because most frames are English, although the Hindi median is 0.545. Read the per-language rows.

### Limits of the dry run

The text is cleanly rendered, high contrast, upright and in one or two fonts. It has no video compression, motion blur, stylised fonts, text over faces, captions burned in by editing apps, or watermarks. Sample sizes are 14 per language. These results cannot rank the engines for real Reels and must not be used to pick one.

## Protocol for the real benchmark

Needs the M0 probe (or any other way to get real frames), which is the owner's step.

1. **Collect 50 frames** from your own account's Reels, after your consent, using the M0 probe export (`spikes/data/frames`). Keep them out of git (`spikes/data/` is ignored).
2. **Make them representative**, not all text-heavy:
   - about 15 with Hindi or Hinglish overlay or caption text, about 15 with English text, about 5 with mixed scripts;
   - about 15 with no overlaid text at all (scenery, faces, products), to measure false text;
   - a spread of styles: subtitles, large meme text, small watermarks and handles, text over busy backgrounds;
   - frames from at least 20 different Reels, at most 2 per Reel (the production limit).
3. **Write ground truth** in `spikes/data/frames/manifest.json`, one entry per frame: `file`, `lang` (`en`, `hi`, `mixed` or `none`), and `text` (the overlay text as read by a person; an empty string when there is none). Label before running, and do not edit labels after seeing results.
4. **Run:**
   ```
   cd spikes/ml-bench
   set HF_HOME=D:\insta_recom_exten\.cache\hf
   .venv\Scripts\python.exe -I ocr_bench.py --frames ..\data\frames --source real --threads 4
   ```
   The result file reports `countsAsBenchmark: true` only for 50 or more real frames.
5. **Record here:** the three tables above for the real run, and per-language failure examples.

### Decision rules, proposed before seeing real data (owner to confirm or change)

- Keep RapidOCR if, on real Hindi and mixed frames, the Devanagari recogniser recovers at least 70% (CER ≤ 0.1), and English recovery is at least 90%.
- Otherwise consider EasyOCR only if it beats RapidOCR by at least 20 points on Hindi and mixed frames and the two-frame cost fits the N3 budget. Since EasyOCR alone is about 6 s per Reel here, that would need a different approach (for example EasyOCR only on frames where RapidOCR returned nothing or low confidence). That would change the architecture and is not decided.
- Choose each engine's confidence threshold as the lowest value at which false text on text-free frames is at most 5% of those frames, then report the recovery cost of that threshold.
- If real false text is high at every usable threshold, OCR text should join the text branch with a low weight or be dropped; that is a separate decision.

## Next steps

1. Owner runs the M0 probe and provides the frames (see `spikes/README.md`).
2. Label them, run the benchmark, fill in this file, and apply the decision rules.
3. Only then design the OCR stage: choose the 2 sharpest frames, apply the per-engine threshold, store `ocr_text` and `ocr_confidence` in `content_features`, run it as an independent stage with a report, and add it to `pipeline_version`.

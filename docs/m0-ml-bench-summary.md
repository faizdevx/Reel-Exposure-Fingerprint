# M0 ML benchmark summary (draft)

Status: partial input to `docs/m0-feasibility-report.md`. That report does not exist yet, and there is no GO / NO-GO decision. This file covers only the CPU model benchmark. The browser experiments (Reel detection, frame capture, metadata, dwell) have not been run.

Source: `spikes/ml-bench/results/bench-4t-20261007-003228.json` (e5) and `bench-4t-20261007-003924.json` (SigLIP, OCR). Run on 2026-10-07 on a 12-thread Intel CPU with 15.7 GB RAM, 4 torch threads, no GPU, Python 3.12.11, torch 2.14.1+cpu, transformers 5.19.0.

## Limits of this data

- All frames are synthetic: 30 images, made from stock photos with overlaid text, plus text cards. They are not Instagram frames. Compression, UI overlays, motion blur and stylised fonts are absent.
- Accuracy samples are tiny: 36 captions, 16 photos, 30 OCR frames. A single miss moves top-1 by 3 to 6 points. Treat the numbers as smoke tests, not estimates.
- The caption set was written by the same author as the taxonomy descriptions, so e5 accuracy is probably optimistic.
- Latency is measured on an idle desktop. A shared 4 vCPU server will be slower.

## Text: multilingual-e5-small

Revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, 384 dimensions.

| Measure | Result |
|---|---|
| Latency, batch of 1 | p50 20 ms, p95 40 ms |
| Zero-shot top-1 / top-3 over 28 topics | 97.2% / 100% (n=36) |
| By language | English 12/12, Hinglish 12/12, Hindi 11/12 top-1 |
| Determinism | identical on repeat; batch 1 vs batched differs by 1e-7 |

The single miss is a Hindi comedy caption about a test, ranked education first, comedy second. Text is cheap and is not a throughput risk.

## Visual: SigLIP

| Model | Revision | p50 per frame | p50 per 5 frames | Zero-shot top-1 / top-3 (n=16) |
|---|---|---|---|---|
| `google/siglip2-base-patch16-224` | `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2` | 217 ms | 807 ms | 68.8% / 93.8% |
| `google/siglip-base-patch16-256-multilingual` | `8952a4eafcde3cb7ab46b1dd629b33f8784ca9c6` | 319 ms | 1199 ms | 87.5% / 87.5% |

Both output 768 dimensions, so the provisional `vector(768)` column holds. Repeat runs are identical.

With n=16, the 87.5% vs 68.8% gap is three images. It does not show that the multilingual model is better. The SigLIP2 model reaches 93.8% top-3, which is what late fusion needs. The multilingual model is about 50% slower.

The `transformers` warning about `bos_token_id` and `eos_token_id` appeared at load. Check that the tokenizer and text tower behave correctly before relying on the text side of either model.

## OCR: RapidOCR

| Config | p50 per frame | English | Hindi (n=14) | Mixed (n=2) |
|---|---|---|---|---|
| Default (PP-OCRv6 small, Chinese and English) | 430 ms | 14/14 exact | 0/14 recovered, median CER 1.0 | 0/2 |
| Devanagari recognizer (PP-OCRv5 mobile) | 472 ms | 14/14 exact | 5/14 recovered, median CER 0.545 | 1/2 |

The default config cannot read Devanagari: it returns empty text or Chinese-looking noise. The Devanagari recognizer reads some Hindi lines correctly but fails on others (empty output, or partial lines such as "मेहनत" instead of the full card). It is usable, not good. The spec fallback is EasyOCR; benchmark it before deciding.

Some Hindi frames are very low contrast or have small text over busy photos, so part of the miss rate may come from the synthetic overlay, not the model. Real frames are needed to know.

## Throughput against N3 (≥1 Reel per 3 s on 4 vCPU)

Estimates built from the p50 numbers above, per Reel, sequential, 4 threads, e5 text added at about 20 ms:

| Frames per Reel | SigLIP2 | Multilingual SigLIP |
|---|---|---|
| 5 | about 3.1–3.3 s | about 3.6 s |
| 3 | about 1.9 s | about 2.2 s |

OCR dominates at 5 frames. The 5-frame case misses N3 by a small margin in the best case. Options, none decided: 3 frames per Reel, OCR on fewer frames than the visual encoder, or two worker processes with fewer threads each. This is a measured-per-component estimate; run the full pipeline end to end before treating it as a result.

## What this settles and what it does not

Settled enough to proceed:

- e5 is fast and accurate enough on this small set to be the text encoder.
- The vector width 768 is valid for both SigLIP candidates.
- The default RapidOCR config is not acceptable for a Hindi-heavy feed.

Still open:

- Which SigLIP model (needs a larger labelled photo set, ideally real frames).
- Whether RapidOCR with the Devanagari recognizer, or EasyOCR, meets the OCR need.
- Frame count per Reel and worker layout, to meet N3.
- Everything that needs a live Instagram session: Reel detection, frame capture method, dwell, metadata.

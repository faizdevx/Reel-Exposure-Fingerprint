# M0 spikes (throwaway)

This folder holds throwaway code for the M0 feasibility spikes. Results go into `docs/m0-feasibility-report.md`. Shipped code must never import anything from here (`CONTRIBUTING.md`).

| Folder | What |
|---|---|
| `extension-probe/` | Unpacked MV3 probe extension. Plain JS, no build step |
| `probe-analysis/` | `summarize_probe.py`: turns an exported probe log into the numbers for the report |
| `ml-bench/` | CPU benchmark for e5, SigLIP and RapidOCR (`bench.py`), and the sample generator (`make_samples.py`) |
| `data/` | **Gitignored.** Holds the probe exports, frames, COCO photos and synthetic frames. It contains third-party Reel content, so never commit it |

## Rules for the browser experiments

- You scroll by hand. Nothing in the probe scrolls, clicks, plays, pauses or navigates.
- The probe reads rendered DOM and `<video>` pixels only. It never reads cookies, credentials, tokens, `localStorage` or network responses, and it calls no Instagram API.
- Everything stays in this browser profile until you export it. The probe makes no network calls.
- Use your own account, and only with your own consent (the consent checkbox in the popup).
- Turn recording off when you are done. **Remove the extension after M0.**

## Run the browser experiments (items 1–10)

### Setup

1. Open `chrome://extensions`, turn on Developer mode, click **Load unpacked**, and select `spikes/extension-probe/`.
2. Pin the extension. Open `https://www.instagram.com/reels/` and log in normally if needed. The probe never sees the login.
3. In the popup:
   - Tick **consent**.
   - Tick **Recording on**.
   - Tick **Save test frames**, so frames are kept for the ML bench.
4. Press **F5** once on the Reels tab. The reload clears the popup's `activeTab` grant, so the automatic capture tests (frame test B) run with host permission only.

A red "M0 probe recording" pill appears top-left, and the extension badge shows `REC`.

### Protocol

Do each run in order, and keep your own count of the Reels you scroll.

**Run 1: baseline.**
1. Scroll **50 Reels** by hand. Watch some for 5+ s and skip some in under 1 s.
2. Enter your count in the popup ("Reels I scrolled by hand").

**Run 2: pause.** On one Reel:
1. Click the video to pause it for about 5 s.
2. Resume it, then scroll on.

**Run 3: tab hidden.** On one Reel:
1. Switch to another tab for about 5 s.
2. Come back, then scroll on.

**Run 4: window unfocused.** On one Reel:
1. Click another window, leaving the Reels tab visible, for about 5 s.
2. Come back, then scroll on.

**Run 5: capture paths.**
1. Open the popup and click **captureVisibleTab now (activeTab)**.
2. Then click **Capture rate test (5 calls)**.

**Run 6: surfaces.**
1. Go to the Instagram home feed, open a Reel from there (the URL becomes `/reel/<code>/`), and scroll a few.
2. Then use the left nav to reach Reels again.

**Run 7: overhead.**
1. Open DevTools > Performance and record while scrolling about 10 Reels.
2. Note dropped frames and long tasks.
3. Save a screenshot of the summary.

**Run 8 (optional): scroll back.** Scroll back up to a Reel you already saw.

### Export

1. Click **Export log JSON** and **Export frames JSON**.
2. Move both files to `spikes/data/` (it is gitignored).

### Summarise

```
python -I spikes/probe-analysis/summarize_probe.py spikes/data/m0-probe-log-<ts>.json \
    --frames spikes/data/m0-probe-frames-<ts>.json --out spikes/data/frames
```

Paste the JSON output into the chat, or into the report under the matching experiment.

### What to check by eye

These checks are recorded in the report.

| Check | How |
|---|---|
| Caption correct | The popup status shows the current exposure. Compare a few exported captions with what is on screen. |
| Handle correct | Compare a few exported handles with what is on screen. |
| Hashtags correct | Compare a few exported hashtags with what is on screen. |
| Captions truncated | Note whether captions are cut at "… more". The probe detects the marker but never clicks it. |
| Frames match the Reel | Open 5 or more exported frames. Each should show the Reel that was playing, not a neighbour, a black frame or UI chrome. |

## Run the ML bench (items 11–14)

Already run on the author's machine. To re-run:

```
cd spikes/ml-bench
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe --index-url https://download.pytorch.org/whl/cpu torch
uv pip install --python .venv/Scripts/python.exe "transformers>=4.49" sentencepiece protobuf pillow numpy rapidocr onnxruntime psutil
# C: has little free space: keep caches on D:
set HF_HOME=D:\insta_recom_exten\.cache\hf
.venv\Scripts\python.exe -I make_samples.py --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe" --tmp <scratch dir>
.venv\Scripts\python.exe -I bench.py --threads 4
```

The COCO photos for `make_samples.py` come from `http://images.cocodataset.org/val2017/<id>.jpg`. The IDs are listed in `make_samples.py`.

To re-run the bench on **real** frames from the probe:

1. Write a `manifest.json` in `spikes/data/frames` with the same fields as the synthetic one: `file`, `topics`, `overlayLang`, `overlay`.
2. Run `bench.py --frames spikes/data/frames`.

### OCR benchmark

`ocr_bench.py` compares RapidOCR (default and Devanagari) with EasyOCR. Install EasyOCR in the bench venv with `uv pip install --python .venv/Scripts/python.exe easyocr`, then run `.venv\Scripts\python.exe -I ocr_bench.py --frames <dir> --source real|synthetic`. Protocol and results: `docs/ocr-evaluation.md`.

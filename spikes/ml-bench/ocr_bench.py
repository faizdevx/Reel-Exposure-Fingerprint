"""OCR benchmark: RapidOCR (default and Devanagari recogniser) against EasyOCR (Hindi + English).

Measures, per engine, on one folder of frames with a `manifest.json`:

- latency per frame (p50, p95, max), after one warm-up call;
- CPU use: CPU seconds spent per wall second over the timed loop (about "cores busy"), and the
  peak working set of the process;
- accuracy against hand-written ground truth, per language (substring edit rate: edit distance of
  the expected text against the best matching part of the OCR output, divided by its length);
- confidence: mean line score, and how accuracy and kept text change as the confidence threshold
  rises (so a threshold can be chosen from data);
- text read from frames that should contain none (false text).

Each engine runs in its own process, so its peak memory and load time are not polluted by the
others.

Manifest entries: `file`, `lang` (`en`, `hi`, `mixed`, ...) and `text` (ground truth; empty string
for a frame with no text). The M0 synthetic manifest uses `overlayLang` and `overlay`; both
spellings are read.

`--source` is recorded in the output and must be `real` or `synthetic`. Only `real` results with at
least 50 frames count as the benchmark of `docs/ocr-evaluation.md`; anything else is a dry run.

    python -I ocr_bench.py --frames ../data/synthetic_frames --source synthetic
    python -I ocr_bench.py --frames ../data/real_frames --source real
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINES = ("rapidocr_default", "rapidocr_devanagari", "easyocr_hi_en")
THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9)
MIN_REAL_FRAMES = 50


def norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).lower().split())


def substring_cer(target: str, text: str) -> float:
    """Edit distance of `target` against the best matching substring of `text`, / len(target)."""
    t, s = norm(target), norm(text)
    if not t:
        return 0.0
    prev = [0] * (len(s) + 1)
    for i in range(1, len(t) + 1):
        cur = [i] + [0] * len(s)
        for j in range(1, len(s) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (t[i - 1] != s[j - 1]))
        prev = cur
    return round(min(prev) / len(t), 3)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def load_manifest(frames: Path) -> list[dict]:
    raw = json.loads((frames / "manifest.json").read_text(encoding="utf-8"))
    return [
        {
            "file": e["file"],
            "lang": e.get("lang", e.get("overlayLang", "unknown")),
            "text": e.get("text", e.get("overlay", "")) or "",
        }
        for e in raw
    ]


# ---------------------------------------------------------------- engines


def make_engine(name: str, threads: int):
    """Return `read(path) -> list[(text, score)]`."""
    if name.startswith("rapidocr"):
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        params = {
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.log_level": "error",
        }
        if name == "rapidocr_devanagari":
            params |= {
                "Rec.lang_type": LangRec.DEVANAGARI,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
            }
        engine = RapidOCR(params=params)

        def read(path: str) -> list[tuple[str, float]]:
            out = engine(path)
            return list(zip(out.txts or [], (float(s) for s in (out.scores or [])), strict=False))

        return read

    import easyocr
    import torch

    torch.set_num_threads(threads)
    reader = easyocr.Reader(["hi", "en"], gpu=False, verbose=False)

    def read(path: str) -> list[tuple[str, float]]:
        return [(text, float(conf)) for _box, text, conf in reader.readtext(path)]

    return read


def run_engine(name: str, frames: Path, threads: int) -> dict:
    import psutil

    os.environ["OMP_NUM_THREADS"] = str(threads)
    manifest = load_manifest(frames)
    proc = psutil.Process()
    result: dict = {"engine": name}

    t0 = time.perf_counter()
    try:
        read = make_engine(name, threads)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    result["loadMs"] = round((time.perf_counter() - t0) * 1000, 1)

    read(str(frames / manifest[0]["file"]))  # warm-up, not timed

    latencies: list[float] = []
    rows: list[dict] = []
    cpu0 = proc.cpu_times()
    wall0 = time.perf_counter()
    for item in manifest:
        t = time.perf_counter()
        try:
            lines = read(str(frames / item["file"]))
            failed = None
        except Exception as error:  # one bad frame must not stop the benchmark
            lines, failed = [], type(error).__name__
        latencies.append((time.perf_counter() - t) * 1000)
        rows.append({**item, "lines": [(t_, round(s, 3)) for t_, s in lines], "failed": failed})
    wall = time.perf_counter() - wall0
    cpu1 = proc.cpu_times()
    cpu_seconds = (cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)

    mem = proc.memory_info()
    result["latencyMs"] = {
        "n": len(latencies),
        "mean": round(statistics.mean(latencies), 1),
        "p50": percentile(latencies, 0.5),
        "p95": percentile(latencies, 0.95),
        "max": round(max(latencies), 1),
    }
    result["cpu"] = {
        "cpuSecondsPerWallSecond": round(cpu_seconds / wall, 2),
        "threadsConfigured": threads,
    }
    result["memory"] = {
        "rssMB": round(mem.rss / 2**20),
        "peakMB": round(getattr(mem, "peak_wset", mem.rss) / 2**20),
    }
    result["failedFrames"] = sum(1 for r in rows if r["failed"])

    labelled = [r for r in rows if r["text"].strip()]
    empty = [r for r in rows if not r["text"].strip()]

    by_threshold: dict = {}
    for threshold in THRESHOLDS:
        per_lang: dict[str, list[float]] = {}
        kept_chars: list[int] = []
        for r in labelled:
            kept = " ".join(t for t, s in r["lines"] if s >= threshold)
            per_lang.setdefault(r["lang"], []).append(substring_cer(r["text"], kept))
            kept_chars.append(len(kept))
        all_cer = [c for values in per_lang.values() for c in values]
        by_threshold[str(threshold)] = {
            "medianCer": round(statistics.median(all_cer), 3) if all_cer else None,
            "recovered": sum(1 for c in all_cer if c <= 0.1),
            "of": len(all_cer),
            "meanKeptChars": round(statistics.mean(kept_chars), 1) if kept_chars else None,
            "falseTextFrames": sum(
                1 for r in empty if any(s >= threshold and t.strip() for t, s in r["lines"])
            ),
        }
    result["byThreshold"] = by_threshold

    langs: dict = {}
    for r in labelled:
        cer = substring_cer(r["text"], " ".join(t for t, _ in r["lines"]))
        scores = [s for _, s in r["lines"]]
        entry = langs.setdefault(r["lang"], {"cer": [], "score": [], "empty": 0})
        entry["cer"].append(cer)
        entry["empty"] += 0 if r["lines"] else 1
        if scores:
            entry["score"].append(statistics.mean(scores))
    result["byLang"] = {
        lang: {
            "n": len(v["cer"]),
            "medianCer": round(statistics.median(v["cer"]), 3),
            "recovered": sum(1 for c in v["cer"] if c <= 0.1),
            "noOutput": v["empty"],
            "meanLineScore": round(statistics.mean(v["score"]), 3) if v["score"] else None,
        }
        for lang, v in langs.items()
    }
    result["framesWithoutText"] = len(empty)
    result["rows"] = rows
    return result


# ---------------------------------------------------------------- driver


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--source", choices=["real", "synthetic"], required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--engines", nargs="*", default=list(ENGINES), choices=ENGINES)
    ap.add_argument("--engine-child", choices=ENGINES, help=argparse.SUPPRESS)
    ap.add_argument("--child-out", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.engine_child:
        data = run_engine(a.engine_child, a.frames, a.threads)
        a.child_out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return

    manifest = load_manifest(a.frames)
    counts_as_benchmark = a.source == "real" and len(manifest) >= MIN_REAL_FRAMES
    results = []
    for name in a.engines:
        with tempfile.TemporaryDirectory() as tmp:
            child_out = Path(tmp) / "out.json"
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--frames",
                    str(a.frames),
                    "--source",
                    a.source,
                    "--threads",
                    str(a.threads),
                    "--engine-child",
                    name,
                    "--child-out",
                    str(child_out),
                ],
                check=True,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            results.append(json.loads(child_out.read_text(encoding="utf-8")))

    import psutil

    out = {
        "startedAt": datetime.now(UTC).isoformat(),
        "frameSource": a.source,
        "nFrames": len(manifest),
        "countsAsBenchmark": counts_as_benchmark,
        "note": None
        if counts_as_benchmark
        else f"DRY RUN: needs source=real and at least {MIN_REAL_FRAMES} frames to count",
        "threads": a.threads,
        "machine": {
            "cpu": platform.processor(),
            "logicalCpus": os.cpu_count(),
            "ramGB": round(psutil.virtual_memory().total / 2**30, 1),
            "python": platform.python_version(),
        },
        "results": results,
    }
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"ocr-{a.source}-{a.threads}t-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()

"""Benchmark of the visual pipeline as shipped (`app.worker.visual_pipeline`), both SigLIP candidates.

Run with the SERVER environment (it has torch, transformers and the app package):

    cd server
    set HF_HOME=D:\\insta_recom_exten\\.cache\\hf
    uv run python ../spikes/ml-bench/visual_bench.py --frames ../spikes/data/synthetic_frames --source synthetic

Measures per model: load time, cold taxonomy-embedding build (text tower over 28 topics) and warm
cache read, latency per frame and per 5-frame Reel (embed_frames, pool, score stages), CPU seconds
per wall second, peak memory, determinism, frame-order independence, and zero-shot topic accuracy
with a temperature sweep against the labelled photos in the manifest.

Accuracy here is a SMOKE TEST: the labelled frames are a handful of stock photos with overlaid text
and the labels are mapped by hand to the taxonomy (below). Only `--source real` with at least 50
labelled frames counts as an evaluation; `docs/visual-evaluation.md` records the difference.
"""

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parents[1] / "server"
sys.path.insert(0, str(SERVER))

# Old M0 label names -> taxonomy slugs that count as correct.
LABEL_MAP = {
    "cricket_sports": ["cricket", "sports_other"],
    "pets_animals": ["pets_animals"],
    "tech_coding": ["ai_programming", "tech_gadgets"],
    "travel": ["travel"],
}
TAUS = (0.01, 0.02, 0.05, 0.1)
MIN_REAL_FRAMES = 50


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 1),
        "p50": round(ordered[len(ordered) // 2], 1),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
        "max": round(ordered[-1], 1),
    }


def run_model(key: str, frames_dir: Path, threads: int) -> dict:
    import psutil

    from app.taxonomy import load_taxonomy
    from app.worker.vision_config import VISION_MODELS, VisualPipelineConfig, VisualScoringConfig
    from app.worker.visual_embedder import SigLipEmbedder
    from app.worker.visual_pipeline import STAGE_EMBED_FRAMES, STAGE_POOL, STAGE_SCORE_VISUAL
    from app.worker.visual_pipeline import VisualPipeline

    taxonomy = load_taxonomy(HERE.parents[1] / "taxonomy" / "topics.json")
    model = replace(VISION_MODELS[key], threads=threads)
    config = VisualPipelineConfig(model)
    manifest = json.loads((frames_dir / "manifest.json").read_text(encoding="utf-8"))
    frames = [(m, (frames_dir / m["file"]).read_bytes()) for m in manifest]
    proc = psutil.Process()
    result: dict = {
        "key": key,
        "model_id": model.model_id,
        "revision": model.revision,
        "license": model.license,
    }

    embedder = SigLipEmbedder(model)
    t0 = time.perf_counter()
    embedder.embed_images([frames[0][1]])  # loads the model
    result["loadAndFirstFrameMs"] = round((time.perf_counter() - t0) * 1000, 1)

    with tempfile.TemporaryDirectory() as cache:
        pipeline = VisualPipeline.create(config, taxonomy, embedder, Path(cache))
        t0 = time.perf_counter()
        pipeline.topics.get()
        result["taxonomyColdBuildMs"] = round((time.perf_counter() - t0) * 1000, 1)
        warm = VisualPipeline.create(config, taxonomy, embedder, Path(cache))
        t0 = time.perf_counter()
        warm.topics.get()
        result["taxonomyWarmCacheMs"] = round((time.perf_counter() - t0) * 1000, 1)

        # latency per single frame (batch of one, as in production)
        per_frame = []
        for _meta, data in frames:
            t0 = time.perf_counter()
            embedder.embed_images([data])
            per_frame.append((time.perf_counter() - t0) * 1000)
        result["latencyPerFrameMs"] = stats(per_frame)

        # latency per Reel: five frames through the whole pipeline
        reels = [[d for _, d in (frames * 2)[i : i + 5]] for i in range(0, max(5, len(frames) - 4), 5)]
        wall, cpu0 = time.perf_counter(), proc.cpu_times()
        per_reel, stage_ms = [], {STAGE_EMBED_FRAMES: [], STAGE_POOL: [], STAGE_SCORE_VISUAL: []}
        for reel in reels:
            t0 = time.perf_counter()
            out = warm.run(reel)
            per_reel.append((time.perf_counter() - t0) * 1000)
            for r in out.reports:
                if r.stage in stage_ms:
                    stage_ms[r.stage].append(r.duration_ms)
        wall = time.perf_counter() - wall
        cpu1 = proc.cpu_times()
        result["latencyPerReelMs"] = stats(per_reel)
        result["framesPerReel"] = 5
        result["stageMeanMs"] = {k: round(statistics.mean(v), 2) for k, v in stage_ms.items()}
        result["cpuSecondsPerWallSecond"] = round(
            ((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)) / wall, 2
        )
        mem = proc.memory_info()
        result["memoryMB"] = {
            "rss": round(mem.rss / 2**20),
            "peak": round(getattr(mem, "peak_wset", mem.rss) / 2**20),
        }

        # determinism and order independence
        sample = [d for _, d in frames[:5]]
        a, b, c = warm.run(sample), warm.run(sample), warm.run(list(reversed(sample)))
        import numpy as np

        result["determinism"] = {
            "repeatMaxAbsDiff": float(np.abs(a.embedding - b.embedding).max()),
            "reversedOrderMaxAbsDiff": float(np.abs(a.embedding - c.embedding).max()),
        }

        # zero-shot accuracy with a temperature sweep (labelled photos only)
        slug_of = {t.id: t.slug for t in taxonomy.topics}
        labelled = [(m, d) for m, d in frames if m["topics"]]
        sweep: dict = {}
        embeddings = {m["file"]: warm.run([d]) for m, d in labelled}
        topic_matrix = warm.topics.get()
        from app.worker.scoring import score_topics

        for tau in TAUS:
            top1 = top3 = 0
            confidences = []
            for m, _d in labelled:
                emb = embeddings[m["file"]].embedding
                scores = score_topics(emb, topic_matrix.topic_ids, topic_matrix.matrix, tau)
                ranked = [slug_of[t] for t, _p in scores.ranked()]
                want = {s for name in m["topics"] for s in LABEL_MAP.get(name, [])}
                top1 += ranked[0] in want
                top3 += bool(set(ranked[:3]) & want)
                confidences.append(scores.ranked()[0][1])
            sweep[str(tau)] = {
                "top1": top1,
                "top3": top3,
                "of": len(labelled),
                "meanTopProbability": round(statistics.mean(confidences), 3),
            }
        result["zeroShotByTau"] = sweep
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--source", choices=["real", "synthetic"], required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--models", nargs="*", default=["siglip2-base", "siglip-multilingual"])
    a = ap.parse_args()

    import psutil

    manifest = json.loads((a.frames / "manifest.json").read_text(encoding="utf-8"))
    labelled = sum(1 for m in manifest if m["topics"])
    out = {
        "startedAt": datetime.now(UTC).isoformat(),
        "frameSource": a.source,
        "nFrames": len(manifest),
        "nLabelled": labelled,
        "countsAsEvaluation": a.source == "real" and labelled >= MIN_REAL_FRAMES,
        "threads": a.threads,
        "machine": {
            "cpu": platform.processor(),
            "logicalCpus": os.cpu_count(),
            "ramGB": round(psutil.virtual_memory().total / 2**30, 1),
            "python": platform.python_version(),
        },
        "results": [run_model(k, a.frames, a.threads) for k in a.models],
    }
    import torch
    import transformers

    out["machine"]["torch"] = torch.__version__
    out["machine"]["transformers"] = transformers.__version__
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    path = results / f"visual-{a.source}-{a.threads}t-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()

"""What one worker does for one session: OCR, text and visual embedding, fusion, database writes.

There is no job consumer yet (`docs/security-audit.md` M-8) and no OCR stage in the application
(`docs/ocr-evaluation.md`), so this suite plays the worker. For every Reel it reads up to two frames
with RapidOCR, then calls the real `process_reel` (text pipeline, visual pipeline, late fusion, the
`other` rule, the writes to `content_features` and `topic_assignments`), one Reel after the other.
After the last Reel it rebuilds the user's fingerprints and predictions once, which is what a
finished session needs. Model load and warm-up are reported apart and are not in the Reel times.

`workers > 1` runs that many processes, each with its own models, its share of the CPUs and of the
Reels (the "two workers with fewer threads each" option of `docs/m0-ml-bench-summary.md`).
"""

import multiprocessing as mp
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bench import data
from app.bench.stats import summarize
from app.bench.suites import database

OCR_MIN_SCORE = 0.5  # lines below this confidence are not kept (docs/ocr-evaluation.md)
FRAME_POOL = 60


@dataclass(frozen=True)
class PipelineJob:
    database_url: str
    taxonomy_file: Path
    model_cache: Path | None
    offline: bool
    visual: str | None  # a key of VISION_MODELS, or None for text only
    ocr: str | None  # "default", "devanagari" or None
    frames_per_reel: int
    ocr_frames: int
    threads: int
    frames_dir: Path | None
    ort_spin: bool = True


def _build(job: PipelineJob) -> Any:
    from app.taxonomy import load_taxonomy
    from app.worker.config import TextModelConfig, TextPipelineConfig
    from app.worker.embedder import E5Embedder
    from app.worker.reel_processor import ReelPipeline
    from app.worker.text_pipeline import TextPipeline
    from app.worker.vision_config import VISION_MODELS, FusionConfig, VisualPipelineConfig
    from app.worker.visual_embedder import SigLipEmbedder
    from app.worker.visual_pipeline import VisualPipeline

    taxonomy = load_taxonomy(job.taxonomy_file)
    text_config = TextPipelineConfig(model=replace(TextModelConfig(), threads=job.threads))
    text = TextPipeline.create(
        text_config,
        taxonomy,
        E5Embedder(text_config.model, cache_dir=job.model_cache, local_files_only=job.offline),
        None,
    )
    visual = None
    if job.visual is not None:
        model = replace(VISION_MODELS[job.visual], threads=job.threads)
        visual = VisualPipeline.create(
            VisualPipelineConfig(model),
            taxonomy,
            SigLipEmbedder(model, cache_dir=job.model_cache, local_files_only=job.offline),
            None,
        )
    return ReelPipeline(text, visual, FusionConfig())


def _ocr_text(engine: Any, frames: list[bytes]) -> str:
    kept: list[str] = []
    for frame in frames:
        result = engine(frame)
        for line, score in zip(result.txts or [], result.scores or [], strict=False):
            if float(score) >= OCR_MIN_SCORE:
                kept.append(str(line))
    return " ".join(kept)


def _worker(
    job: PipelineJob,
    user_id: uuid.UUID,
    indices: list[int],
    cpu_ids: list[int] | None,
    ready: Any,
    start: Any,
    queue: Any,
) -> None:
    try:
        _work(job, user_id, indices, cpu_ids, ready, start, queue)
    except BaseException as error:  # the parent must hear about it, not wait for a result
        queue.put({"error": f"{type(error).__name__}: {error}"})
        ready.set()
        raise


def _work(
    job: PipelineJob,
    user_id: uuid.UUID,
    indices: list[int],
    cpu_ids: list[int] | None,
    ready: Any,
    start: Any,
    queue: Any,
) -> None:
    import psutil

    from app.bench.suites.models import make_ocr
    from app.worker.reel_processor import process_reel
    from app.worker.text_normalize import TextInput

    if cpu_ids:
        psutil.Process().cpu_affinity(cpu_ids)
    load_started = time.perf_counter()
    pipeline = _build(job)
    engine = make_ocr(job.ocr, job.threads, spin=job.ort_spin) if job.ocr else None
    pool = data.load_frames(job.frames_dir, FRAME_POOL)
    if engine is not None:
        engine(pool[0])
    captions = data.captions((max(indices) + 1) if indices else 1)

    with Session(database.engine_for(job.database_url)) as db:
        # Warm-up: model load, first-call costs, the topic embeddings. Not counted.
        process_reel(
            db,
            pipeline,
            user_id,
            f"warm{uuid.uuid4().hex[:8]}",
            TextInput("warm up caption", ("warm",), None),
            pool[: job.frames_per_reel] if job.visual else (),
        )
        load_ms = (time.perf_counter() - load_started) * 1000.0
        ready.set()
        start.wait()

        rows: list[dict[str, Any]] = []
        for i in indices:
            frames = [
                pool[(i * job.frames_per_reel + k) % FRAME_POOL] for k in range(job.frames_per_reel)
            ]
            caption, tags = captions[i]
            t0 = time.perf_counter()
            ocr_text = _ocr_text(engine, frames[: job.ocr_frames]) if engine else ""
            t1 = time.perf_counter()
            outcome = process_reel(
                db,
                pipeline,
                user_id,
                f"Bench{i:05d}",
                TextInput(caption, tags, ocr_text or None),
                frames if job.visual else (),
            )
            t2 = time.perf_counter()
            rows.append(
                {
                    "i": i,
                    "ocr_ms": (t1 - t0) * 1000.0,
                    "process_ms": (t2 - t1) * 1000.0,
                    "total_ms": (t2 - t0) * 1000.0,
                    "stages": {r.stage: r.duration_ms for r in outcome.reports},
                    "failed": [r.stage for r in outcome.reports if r.status == "failed"],
                }
            )
        peak = psutil.Process().memory_info().rss / 2**20
    queue.put(
        {
            "rows": rows,
            "peak_rss_mb": round(peak, 1),
            "finished": time.perf_counter(),
            "load_and_warmup_ms": round(load_ms, 1),
        }
    )


def _seed_user(url: str, reels: int) -> uuid.UUID:
    from app.models import ReelExposure, User
    from app.models import Session as MonitoringSession

    user = uuid.uuid4()
    session_id = uuid.uuid4()
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    captions = data.captions(reels)
    with Session(database.engine_for(url)) as db:
        db.add(User(id=user, email="bench@example.test", password_hash="x"))
        db.flush()
        db.add(
            MonitoringSession(
                id=session_id,
                user_id=user,
                started_at=start,
                ended_at=start + timedelta(minutes=59),
                status="stopped",
                stop_reason="user",
            )
        )
        db.flush()
        for i in range(reels):
            caption, tags = captions[i]
            db.add(
                ReelExposure(
                    event_id=uuid.uuid4(),
                    session_id=session_id,
                    user_id=user,
                    reel_shortcode=f"Bench{i:05d}",
                    observed_at=start + timedelta(seconds=i * 15),
                    dwell_ms=4000 + (i * 37) % 9000,
                    video_duration_ms=15000,
                    caption=caption,
                    hashtags=list(tags),
                    field_status={},
                    adapter_version="bench",
                )
            )
        db.commit()
    return user


def run(*, job: PipelineJob, reels: int, workers: int, cpus: int | None) -> dict[str, Any]:
    import psutil

    from app.fingerprint.engine import FingerprintConfig
    from app.fingerprint.store import rebuild_user
    from app.models import TopicAssignment
    from app.prediction.config import PredictionConfig
    from app.prediction.store import rebuild_predictions
    from app.taxonomy import load_taxonomy
    from app.worker.store import sync_topics

    engine = database.reset(job.database_url)
    taxonomy = load_taxonomy(job.taxonomy_file)
    with Session(engine) as db:
        sync_topics(db, taxonomy)
        db.commit()
    user = _seed_user(job.database_url, reels)

    threads = job.threads if workers == 1 else max(1, job.threads // workers)
    logical = list(psutil.Process().cpu_affinity())
    groups: list[list[int] | None] = [None] * workers
    if cpus:
        per = max(1, len(logical) // workers)
        groups = [logical[w * per : (w + 1) * per] for w in range(workers)]
    shares = [list(range(w, reels, workers)) for w in range(workers)]

    ctx = mp.get_context("spawn")
    start = ctx.Event()
    queue: Any = ctx.Queue()
    readies = [ctx.Event() for _ in range(workers)]
    procs = [
        ctx.Process(
            target=_worker,
            args=(
                replace(job, threads=threads),
                user,
                shares[w],
                groups[w],
                readies[w],
                start,
                queue,
            ),
        )
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for r in readies:
        r.wait(timeout=1800)
    began = time.perf_counter()
    start.set()
    results: list[dict[str, Any]] = []
    while len(results) < len(procs):
        try:
            results.append(queue.get(timeout=5))
        except Exception:  # queue.Empty: look for a worker that died without saying so
            if all(not p.is_alive() for p in procs) and len(results) < len(procs):
                raise RuntimeError("a benchmark worker exited without a result") from None
    for p in procs:
        p.join()
    errors = [r["error"] for r in results if "error" in r]
    if errors:
        raise RuntimeError(f"a benchmark worker failed: {errors[0]}")
    wall = max(r["finished"] for r in results) - began

    with Session(engine) as db:
        version = db.execute(select(TopicAssignment.pipeline_version).limit(1)).scalar_one()
        ids = [t.id for t in taxonomy.topics]
        t0 = time.perf_counter()
        rebuild_user(
            db, user, assignment_version=version, topic_ids=ids, config=FingerprintConfig()
        )
        db.commit()
        t1 = time.perf_counter()
        rebuild_predictions(
            db,
            user,
            assignment_version=version,
            fp_config=FingerprintConfig(),
            config=PredictionConfig(),
        )
        db.commit()
        t2 = time.perf_counter()

    rows = [row for r in results for row in r["rows"]]
    names = sorted({s for row in rows for s in row["stages"]})
    return {
        "reels": len(rows),
        "workers": workers,
        "threads_per_worker": threads,
        "visual_model": job.visual,
        "ocr": job.ocr,
        "ort_spinning": job.ort_spin,
        "frames_per_reel": job.frames_per_reel,
        "ocr_frames_per_reel": job.ocr_frames if job.ocr else 0,
        "wall_seconds_for_all_reels": round(wall, 2),
        "seconds_per_reel_wall": round(wall / max(1, len(rows)), 3),
        "reels_per_second": round(len(rows) / wall, 4),
        "fingerprint_rebuild_seconds": round(t1 - t0, 3),
        "prediction_rebuild_seconds": round(t2 - t1, 3),
        "total_with_post_seconds": round(wall + (t2 - t0), 2),
        "per_reel_total_ms": summarize([r["total_ms"] for r in rows]),
        "per_reel_ocr_ms": summarize([r["ocr_ms"] for r in rows]),
        "per_reel_process_ms": summarize([r["process_ms"] for r in rows]),
        "stage_ms": {
            s: summarize([r["stages"][s] for r in rows if s in r["stages"]]) for s in names
        },
        "failed_stages": sorted({s for row in rows for s in row["failed"]}),
        "peak_rss_mb_per_worker": [r["peak_rss_mb"] for r in results],
        "load_and_warmup_ms_per_worker": [r["load_and_warmup_ms"] for r in results],
        "cpu_ids_per_worker": groups,
    }

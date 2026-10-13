"""Model latency: text embedding (e5), visual embedding (SigLIP) and OCR (RapidOCR).

Each measurement is the wall time of one call after warm-up, on one thread pool of `threads`
threads (the spec's 4 vCPU), one input at a time, as the worker runs them. Model load time and the
process's peak memory are reported next to the latency. Inputs are generated (`app.bench.data`).
"""

import io
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import psutil

from app.bench import data
from app.bench.stats import repeat_ms, summarize, timed
from app.taxonomy import load_taxonomy
from app.worker.config import TextModelConfig, TextPipelineConfig
from app.worker.embedder import E5Embedder
from app.worker.stages import StageRunner
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import VISION_MODELS, VisualPipelineConfig
from app.worker.visual_embedder import SigLipEmbedder
from app.worker.visual_pipeline import VisualPipeline


def _two(read: Callable[[bytes], Any], a: bytes, b: bytes) -> list[Any]:
    return [read(a), read(b)]


def _each(items: Sequence[Any], fn: Callable[[Any], Any]) -> Callable[[], Any]:
    """A call with no arguments that applies `fn` to the next item each time it is called."""
    source = iter(items)
    return lambda: fn(next(source))


def rss_mb() -> float:
    return float(round(psutil.Process().memory_info().rss / 2**20, 1))


def text_embedding(
    *, n: int, threads: int, taxonomy_file: Path, model_cache: Path | None, offline: bool
) -> dict[str, Any]:
    config = TextPipelineConfig(model=replace(TextModelConfig(), threads=threads))
    embedder = E5Embedder(config.model, cache_dir=model_cache, local_files_only=offline)
    taxonomy = load_taxonomy(taxonomy_file)
    texts = [c for c, _ in data.captions(n + 5)]

    _, load_ms = timed(lambda: embedder.embed(["query: warm"], "query"))
    embed = repeat_ms(_each(texts * 2, lambda text: embedder.embed([text], "query")), n)

    pipeline, build_ms = timed(lambda: TextPipeline.create(config, taxonomy, embedder, None))
    caps = data.captions(n)
    pipe = []
    for caption, tags in caps:
        runner = StageRunner()
        _, ms = timed(partial(pipeline.run, TextInput(caption, tags, None), runner=runner))
        pipe.append(ms)
    return {
        "model": config.model.model_id,
        "revision": config.model.revision,
        "threads": threads,
        "first_call_ms": round(load_ms, 1),
        "taxonomy_embedding_build_ms": round(build_ms, 1),
        "embed_one_text_ms": summarize(embed),
        "whole_text_stage_ms": summarize(pipe),
        "peak_rss_mb": rss_mb(),
        "inputs": "generated English, Hinglish and Hindi captions with hashtags",
    }


def visual_embedding(
    *,
    key: str,
    n_frames: int,
    threads: int,
    taxonomy_file: Path,
    model_cache: Path | None,
    offline: bool,
    frames_dir: Path | None,
) -> dict[str, Any]:
    model = replace(VISION_MODELS[key], threads=threads)
    config = VisualPipelineConfig(model)
    embedder = SigLipEmbedder(model, cache_dir=model_cache, local_files_only=offline)
    frames = data.load_frames(frames_dir, n_frames)

    _, load_ms = timed(lambda: embedder.embed_images([frames[0]]))
    per_frame = repeat_ms(_each(frames * 2, lambda f: embedder.embed_images([f])), n_frames)

    taxonomy = load_taxonomy(taxonomy_file)
    pipeline, build_ms = timed(lambda: VisualPipeline.create(config, taxonomy, embedder, None))
    runner = StageRunner()
    pipeline.run(frames[:5], runner=runner)  # warm: builds the visual topic embeddings
    per_reel: dict[str, Any] = {}
    for count in (3, 5):
        samples = []
        for start in range(0, max(1, n_frames - count), max(1, count)):
            batch = frames[start : start + count]
            if len(batch) < count:
                break
            _, ms = timed(partial(pipeline.run, batch, runner=StageRunner()))
            samples.append(ms)
        per_reel[f"{count}_frames"] = summarize(samples)
    return {
        "model": model.model_id,
        "revision": model.revision,
        "key": key,
        "threads": threads,
        "first_call_ms": round(load_ms, 1),
        "visual_topic_embedding_build_ms": round(build_ms, 1),
        "embed_one_frame_ms": summarize(per_frame),
        "whole_visual_stage_per_reel_ms": per_reel,
        "peak_rss_mb": rss_mb(),
        "inputs": "generated 540x960 JPEG frames"
        if frames_dir is None
        else f"JPEGs of {frames_dir}",
    }


def _stop_ort_spinning() -> None:
    """Make every ONNX Runtime session built from now on sleep instead of spinning when idle.

    RapidOCR does not expose this option, so its session factory is wrapped. After a call, ONNX
    Runtime's worker threads busy-wait for a while; on a few CPUs shared with PyTorch that steals
    the cycles of the next model (`app.bench contention`).
    """
    from rapidocr.inference_engine.onnxruntime.main import OrtInferSession

    original = OrtInferSession._init_sess_opts
    if getattr(original, "_no_spin", False):
        return

    def patched(cfg: Any) -> Any:
        options = original(cfg)
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return options

    patched._no_spin = True  # type: ignore[attr-defined]
    OrtInferSession._init_sess_opts = staticmethod(patched)  # type: ignore[method-assign]


def make_ocr(kind: str, threads: int, *, spin: bool = True) -> Any:
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    if not spin:
        _stop_ort_spinning()

    params: dict[str, Any] = {
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.log_level": "error",
    }
    if kind == "devanagari":
        params |= {
            "Rec.lang_type": LangRec.DEVANAGARI,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
        }
    return RapidOCR(params=params)


def ocr(*, kind: str, n_frames: int, threads: int, frames_dir: Path | None) -> dict[str, Any]:
    frames = data.load_frames(frames_dir, n_frames)
    started = time.perf_counter()
    engine = make_ocr(kind, threads)
    engine(frames[0])  # first call builds the sessions
    load_ms = (time.perf_counter() - started) * 1000.0

    def read(frame: bytes) -> int:
        result = engine(frame)
        return len(result.txts or [])

    per_frame = repeat_ms(_each(frames * 2, read), n_frames)
    # The spec reads at most two frames per Reel (docs/ocr-evaluation.md).
    per_reel = []
    for start in range(0, n_frames - 1, 2):
        _, ms = timed(partial(_two, read, frames[start], frames[start + 1]))
        per_reel.append(ms)
    lines = [read(f) for f in frames[: min(20, n_frames)]]
    return {
        "engine": f"rapidocr-{kind}",
        "threads": threads,
        "first_call_ms": round(load_ms, 1),
        "one_frame_ms": summarize(per_frame),
        "two_frames_per_reel_ms": summarize(per_reel),
        "mean_text_lines_per_frame": round(sum(lines) / max(1, len(lines)), 2),
        "peak_rss_mb": rss_mb(),
        "inputs": "generated 540x960 JPEG frames with English overlay text"
        if frames_dir is None
        else f"JPEGs of {frames_dir}",
    }


def decode_cost(frames: list[bytes]) -> dict[str, Any]:
    """JPEG decode of one frame, for reference (it is inside every number above)."""
    from PIL import Image

    def decode() -> None:
        Image.open(io.BytesIO(frames[0])).convert("RGB").load()

    return summarize(repeat_ms(decode, 50))

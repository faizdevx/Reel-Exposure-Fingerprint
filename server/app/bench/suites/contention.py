"""Does one model slow the next one in the same process?

In the end-to-end run the text embedding took about 78 ms per Reel, against about 11 ms when it is
timed alone (`models` suite). The worker runs OCR (ONNX Runtime), the text model and the image model
(both PyTorch) one after the other in one process, on the same four CPUs. This suite times the same
text embedding in four situations to find out whether the neighbours are the cause:

    idle          the text model alone, call after call
    after_ocr     right after one OCR call on a frame
    after_ocr_with_ort_spinning_off   the same, with ONNX Runtime told not to spin when idle
    after_vision  right after five frames through SigLIP
    after_ocr_and_vision   after both, as in the pipeline

Each is repeated `n` times; only the text call is timed.
"""

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.bench import data
from app.bench.stats import summarize
from app.bench.suites.models import make_ocr
from app.worker.config import TextModelConfig
from app.worker.embedder import E5Embedder
from app.worker.vision_config import VISION_MODELS
from app.worker.visual_embedder import SigLipEmbedder


def run(
    *, n: int, threads: int, model_cache: Path | None, offline: bool, visual: str
) -> dict[str, Any]:
    text = E5Embedder(
        replace(TextModelConfig(), threads=threads), cache_dir=model_cache, local_files_only=offline
    )
    vision = SigLipEmbedder(
        replace(VISION_MODELS[visual], threads=threads),
        cache_dir=model_cache,
        local_files_only=offline,
    )
    ocr = make_ocr("devanagari", threads)
    ocr_quiet = make_ocr(
        "devanagari", threads, spin=False
    )  # built after the first: only it is patched
    frames = data.load_frames(None, 10)
    captions = [c for c, _ in data.captions(n + 10)]
    text.embed(["query: warm"], "query")
    vision.embed_images([frames[0]])
    ocr(frames[0])
    ocr_quiet(frames[0])

    def timed_text(i: int) -> float:
        t0 = time.perf_counter()
        text.embed([captions[i]], "query")
        return (time.perf_counter() - t0) * 1000.0

    def ocr_once() -> None:
        ocr(frames[1])

    def quiet_ocr_once() -> None:
        ocr_quiet(frames[1])

    def vision_five() -> None:
        for frame in frames[:5]:
            vision.embed_images([frame])

    def nothing() -> None:
        return None

    def ocr_and_vision() -> None:
        ocr_once()
        vision_five()

    conditions: dict[str, Callable[[], None]] = {
        "idle": nothing,
        "after_ocr": ocr_once,
        "after_ocr_with_ort_spinning_off": quiet_ocr_once,
        "after_vision": vision_five,
        "after_ocr_and_vision": ocr_and_vision,
    }
    out: dict[str, Any] = {}
    for name, before in conditions.items():
        samples = []
        for i in range(n):
            before()
            samples.append(timed_text(i))
        out[name] = summarize(samples)
    # The same, with a short pause between the neighbour and the text call.
    paused = []
    for i in range(n):
        ocr_once()
        time.sleep(0.25)
        paused.append(timed_text(i))
    out["after_ocr_then_250ms_pause"] = summarize(paused)
    return {"threads": threads, "visual_model": visual, "text_embed_ms_by_situation": out}

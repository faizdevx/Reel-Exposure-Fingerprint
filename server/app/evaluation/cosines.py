"""Cosine cache: run the real pipelines once per labelled Reel, keep cosines, not probabilities.

`score = cos(Reel, topic)` per modality does not depend on any temperature, so every temperature
and weight in the sweep is computed from this cache with no model call. A Reel without text, or
without frames, or whose stage failed, has NaN for that modality: missing stays missing.

The cache file records the labelled set's hash, the text pipeline version, the visual model (id,
revision, licence) and the taxonomy topic order, so a stale or mismatched cache is refused instead
of silently evaluated.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from app.evaluation.labels import LabelledReel
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import TextPipeline
from app.worker.visual_pipeline import VisualPipeline

Matrix = npt.NDArray[np.float64]
FORMAT = "cosine-cache/1"


class CacheError(Exception):
    """The cache does not match what is being evaluated."""


@dataclass(frozen=True)
class CosineSet:
    ids: tuple[str, ...]
    groups: tuple[str, ...]
    label_ids: npt.NDArray[np.int64]
    topic_ids: tuple[int, ...]
    cos_text: Matrix
    cos_visual: Matrix
    meta: dict[str, object]

    def __post_init__(self) -> None:
        n, k = len(self.ids), len(self.topic_ids)
        if self.cos_text.shape != (n, k) or self.cos_visual.shape != (n, k):
            raise CacheError("cosine matrices do not match the Reels and topics")
        if len(self.groups) != n or len(self.label_ids) != n:
            raise CacheError("labels and groups do not match the Reels")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_cosines(
    reels: list[LabelledReel],
    text: TextPipeline,
    visual: VisualPipeline | None,
    *,
    labels_sha256: str,
    visual_meta: dict[str, object] | None,
) -> CosineSet:
    topic_ids = tuple(t.id for t in text.taxonomy.topics)
    n, k = len(reels), len(topic_ids)
    cos_text = np.full((n, k), np.nan)
    cos_visual = np.full((n, k), np.nan)
    for i, reel in enumerate(reels):
        text_result = text.run(
            TextInput(caption=reel.caption, hashtags=reel.hashtags, ocr_text=reel.ocr_text)
        )
        if text_result.scores is not None:
            cos_text[i] = text_result.scores.cosines
        if visual is not None and reel.frames:
            visual_result = visual.run([f.read_bytes() for f in reel.frames])
            if visual_result.scores is not None:
                cos_visual[i] = visual_result.scores.cosines
    return CosineSet(
        ids=tuple(r.id for r in reels),
        groups=tuple(r.group for r in reels),
        label_ids=np.asarray([r.label_id for r in reels], dtype=np.int64),
        topic_ids=topic_ids,
        cos_text=cos_text,
        cos_visual=cos_visual,
        meta={
            "format": FORMAT,
            "labels_sha256": labels_sha256,
            "text_pipeline_version": text.version,
            "text_tau_default": text.config.scoring.tau_text,
            "visual": visual_meta,
            "visual_tau_default": None if visual is None else visual.config.scoring.tau_visual,
        },
    )


def save_cosines(data: CosineSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        ids=np.asarray(data.ids),
        groups=np.asarray(data.groups),
        label_ids=data.label_ids,
        topic_ids=np.asarray(data.topic_ids, dtype=np.int64),
        cos_text=data.cos_text,
        cos_visual=data.cos_visual,
        meta=np.asarray(json.dumps(data.meta, sort_keys=True)),
    )


def load_cosines(path: Path, *, labels_sha256: str | None = None) -> CosineSet:
    with np.load(path, allow_pickle=False) as raw:
        meta = json.loads(str(raw["meta"]))
        data = CosineSet(
            ids=tuple(str(x) for x in raw["ids"]),
            groups=tuple(str(x) for x in raw["groups"]),
            label_ids=raw["label_ids"].astype(np.int64),
            topic_ids=tuple(int(x) for x in raw["topic_ids"]),
            cos_text=raw["cos_text"].astype(np.float64),
            cos_visual=raw["cos_visual"].astype(np.float64),
            meta=meta,
        )
    if meta.get("format") != FORMAT:
        raise CacheError("unknown cache format")
    if labels_sha256 is not None and meta.get("labels_sha256") != labels_sha256:
        raise CacheError("the cache was built from a different labelled set")
    return data

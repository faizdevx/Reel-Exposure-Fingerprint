"""The hand-labelled set: one JSON object per line.

    {"id": "r001", "label": "cricket", "group": "source-a",
     "caption": "text as shown", "hashtags": ["ipl"], "ocr_text": null,
     "frames": ["frames/r001_0.jpg", "frames/r001_1.jpg"], "annotator": "A"}

- `label`: a taxonomy slug, or `other` for a Reel that fits no topic. One label per Reel; the
  annotator decides before looking at any model output.
- `group`: who or what the Reel came from (a user, an account, a session). Reels of one group are
  split together, so the held-out part is not a copy of the development part.
- `caption`, `hashtags`, `ocr_text` and `frames` are what the pipeline sees; any of them may be
  missing (null or absent, an empty list for no frames). Missing means missing: nothing is filled
  in. `frames` are paths relative to the labels file, at most 5.
- Real people and handles must not appear: use the same scrubbed text rules as the test fixtures.

Never commit a labelled set made from real Reels (CONTRIBUTING.md git rules).
"""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.taxonomy import Taxonomy
from app.worker.fusion import OTHER_SLUG, OTHER_TOPIC_ID
from app.worker.vision_config import MAX_FRAMES_PER_REEL


class LabelFileError(Exception):
    """The labelled set is unusable. The message names the line, never its content."""


@dataclass(frozen=True)
class LabelledReel:
    id: str
    label_slug: str
    #: Taxonomy topic id of the label, or 0 for `other`.
    label_id: int
    group: str
    caption: str | None
    hashtags: tuple[str, ...] | None
    ocr_text: str | None
    frames: tuple[Path, ...]
    annotator: str | None


def _text(value: object, field: str, line: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LabelFileError(f"line {line}: {field} must be a string or null")
    return value


def load_labels(path: Path, taxonomy: Taxonomy, *, check_frames: bool = True) -> list[LabelledReel]:
    slug_to_id = {t.slug: t.id for t in taxonomy.topics} | {OTHER_SLUG: OTHER_TOPIC_ID}
    reels: list[LabelledReel] = []
    seen: set[str] = set()
    base = path.parent
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            raise LabelFileError(f"line {number}: not valid JSON") from None
        if not isinstance(row, dict):
            raise LabelFileError(f"line {number}: expected an object")
        unknown = set(row) - {
            "id",
            "label",
            "group",
            "caption",
            "hashtags",
            "ocr_text",
            "frames",
            "annotator",
            "notes",
        }
        if unknown:
            raise LabelFileError(f"line {number}: unknown fields {sorted(unknown)}")
        reel_id = row.get("id")
        if not isinstance(reel_id, str) or not reel_id or reel_id in seen:
            raise LabelFileError(f"line {number}: id must be a unique non-empty string")
        seen.add(reel_id)
        label = row.get("label")
        if label not in slug_to_id:
            raise LabelFileError(f"line {number}: label is not a taxonomy slug or 'other'")
        group = row.get("group")
        if not isinstance(group, str) or not group:
            raise LabelFileError(f"line {number}: group is required")
        hashtags = row.get("hashtags")
        if hashtags is not None and not (
            isinstance(hashtags, list) and all(isinstance(h, str) for h in hashtags)
        ):
            raise LabelFileError(f"line {number}: hashtags must be a list of strings or null")
        frames_raw = row.get("frames") or []
        if not isinstance(frames_raw, list) or not all(isinstance(f, str) for f in frames_raw):
            raise LabelFileError(f"line {number}: frames must be a list of paths")
        if len(frames_raw) > MAX_FRAMES_PER_REEL:
            raise LabelFileError(f"line {number}: at most {MAX_FRAMES_PER_REEL} frames")
        frames = tuple((base / f) for f in frames_raw)
        for frame in frames:
            if check_frames and not frame.is_file():
                raise LabelFileError(f"line {number}: a frame file does not exist")
            if base.resolve() not in frame.resolve().parents:
                raise LabelFileError(f"line {number}: frames must stay under the labels folder")
        reels.append(
            LabelledReel(
                id=reel_id,
                label_slug=str(label),
                label_id=slug_to_id[str(label)],
                group=group,
                caption=_text(row.get("caption"), "caption", number),
                hashtags=None if hashtags is None else tuple(hashtags),
                ocr_text=_text(row.get("ocr_text"), "ocr_text", number),
                frames=frames,
                annotator=_text(row.get("annotator"), "annotator", number),
            )
        )
    if not reels:
        raise LabelFileError("the labelled set is empty")
    return reels


def label_counts(reels: list[LabelledReel]) -> dict[str, int]:
    return dict(sorted(Counter(r.label_slug for r in reels).items()))

"""Late fusion at the probability level (spec §5.2).

    p(k) = sum_m w_m * p_m(k) / sum_m w_m        over the modalities that exist for this Reel

Only probabilities are combined. The text vector (e5, 384 numbers) and the image vector (SigLIP,
768 numbers) are in unrelated spaces and are never added or averaged. A modality that is missing
for a Reel (no text, no frames, or its stage failed) drops out and the remaining weights are
renormalised, so a Reel with frames but no caption still gets a distribution (spec F9).

The catch-all `other` of spec §5.2 is decided afterwards, from the fused distribution
(`decide_other`): if its largest probability is below `FusionConfig.other_threshold`, the Reel is
assigned to `other`. The threshold has no default; it is set from the labelled set
(`app.evaluation`).
"""

import math
from collections.abc import Mapping

from app.worker.vision_config import FusionConfig


class FusionError(Exception):
    """The inputs cannot be fused (different topic sets, or not a distribution)."""


def _check(name: str, probs: Mapping[int, float]) -> None:
    if not probs:
        raise FusionError(f"{name} distribution is empty")
    if any(not (math.isfinite(p) and p >= 0.0) for p in probs.values()):
        raise FusionError(f"{name} has a probability that is not a finite non-negative number")
    if abs(math.fsum(probs.values()) - 1.0) > 1e-6:
        raise FusionError(f"{name} probabilities do not sum to 1")


def fuse(
    p_text: Mapping[int, float] | None,
    p_visual: Mapping[int, float] | None,
    config: FusionConfig,
) -> dict[int, float] | None:
    """The fused distribution, or None when neither modality is present."""
    present: list[tuple[float, Mapping[int, float]]] = []
    if p_text is not None:
        _check("text", p_text)
        present.append((config.w_text, p_text))
    if p_visual is not None:
        _check("visual", p_visual)
        present.append((config.w_visual, p_visual))
    if not present:
        return None
    topics = set(present[0][1])
    if any(set(p) != topics for _, p in present):
        raise FusionError("modalities score different topic sets")
    total = math.fsum(w for w, _ in present)
    ids = sorted(topics)
    fused = {k: math.fsum(w * p[k] for w, p in present) / total for k in ids}
    # Renormalise away the last bits of rounding so the stored column sums to 1.
    norm = math.fsum(fused.values())
    return {k: v / norm for k, v in fused.items()}


#: Topic id of the catch-all `other`. Taxonomy topics start at 1, so 0 never collides.
OTHER_TOPIC_ID = 0
OTHER_SLUG = "other"
OTHER_LABEL = "Other or unclear"


def decide_other(p_fused: Mapping[int, float], threshold: float | None) -> bool:
    """True when no topic is likely enough: `max_k p(k) < threshold`. Never True without one."""
    return threshold is not None and max(p_fused.values()) < threshold


def assign_other(p_fused: Mapping[int, float]) -> dict[int, float]:
    """The distribution of a Reel assigned to `other`: all of it on `other`, none on the topics."""
    if OTHER_TOPIC_ID in p_fused:
        raise FusionError("topic id 0 is reserved for other")
    return {**{k: 0.0 for k in sorted(p_fused)}, OTHER_TOPIC_ID: 1.0}

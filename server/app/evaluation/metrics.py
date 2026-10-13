"""Probabilities from cosines, late fusion on arrays, metrics, and cluster bootstrap intervals.

All functions work on whole labelled sets as numpy arrays (rows are Reels, columns are taxonomy
topics in a fixed order). A row of NaN means the modality does not exist for that Reel.

`fuse_rows` is the vectorised twin of `app.worker.fusion.fuse` (a test checks they agree): only
probabilities are combined, weights are renormalised over the modalities a Reel has, and a Reel
with neither gets a NaN row.
"""

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from app.worker.fusion import OTHER_TOPIC_ID

Matrix = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]

_LOG_FLOOR = 1e-12


def softmax_rows(cosines: Matrix, tau: float) -> Matrix:
    """`softmax_k(cos_k / tau)` per row; NaN rows stay NaN."""
    if not 0.0 < tau <= 1.0:
        raise ValueError("tau must be in (0, 1]")
    out: Matrix = np.full_like(cosines, np.nan)
    valid = ~np.isnan(cosines).any(axis=1)
    if bool(valid.any()):
        z = cosines[valid] / tau
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        out[valid] = e / e.sum(axis=1, keepdims=True)
    return out


def fuse_rows(p_text: Matrix, p_visual: Matrix, w_text: float, w_visual: float) -> Matrix:
    """`sum_m w_m p_m / sum_m w_m` over the modalities each Reel has."""
    has_t = ~np.isnan(p_text).any(axis=1)
    has_v = ~np.isnan(p_visual).any(axis=1)
    wt = np.where(has_t, w_text, 0.0)[:, None]
    wv = np.where(has_v, w_visual, 0.0)[:, None]
    total = wt + wv
    numerator = wt * np.nan_to_num(p_text) + wv * np.nan_to_num(p_visual)
    with np.errstate(invalid="ignore", divide="ignore"):
        fused: Matrix = numerator / total
    fused[total[:, 0] == 0.0] = np.nan
    return fused


@dataclass(frozen=True)
class PerReel:
    """Per-Reel values for the Reels a metric covers (a mask says which)."""

    covered: BoolArray
    top1: Matrix
    top3: Matrix
    loss: Matrix


def per_reel(probs: Matrix, label_ids: IntArray, topic_ids: tuple[int, ...]) -> PerReel:
    """Top-1, top-3 and log loss per Reel, over Reels that have a distribution and a topic label.

    A Reel labelled `other` has no topic to be right about, so it is left out here; `other` is
    scored by `other_accuracy`.
    """
    topics = np.asarray(topic_ids, dtype=np.int64)
    has_p = ~np.isnan(probs).any(axis=1)
    covered = has_p & (label_ids != OTHER_TOPIC_ID)
    n = len(label_ids)
    top1 = np.zeros(n)
    top3 = np.zeros(n)
    loss = np.zeros(n)
    for i in np.flatnonzero(covered):
        order = np.lexsort((topics, -probs[i]))  # highest first, ties to the lower topic id
        ranked = topics[order]
        top1[i] = float(ranked[0] == label_ids[i])
        top3[i] = float(label_ids[i] in ranked[:3])
        p_true = probs[i, int(np.flatnonzero(topics == label_ids[i])[0])]
        loss[i] = -math.log(max(float(p_true), _LOG_FLOOR))
    return PerReel(covered, top1, top3, loss)


def other_accuracy(
    probs: Matrix, label_ids: IntArray, topic_ids: tuple[int, ...], threshold: float | None
) -> tuple[BoolArray, Matrix, Matrix]:
    """Scores with `other` as a class. Returns (covered, correct, assigned) per Reel.

    The prediction is `other` when the largest probability is under the threshold, else the top
    topic. `correct` is 1 when the prediction equals the label (so an `other` label is right only
    when `other` was predicted). `assigned` is 1 when a topic, not `other`, was predicted.
    """
    topics = np.asarray(topic_ids, dtype=np.int64)
    covered = ~np.isnan(probs).any(axis=1)
    n = len(label_ids)
    correct = np.zeros(n)
    assigned = np.zeros(n)
    for i in np.flatnonzero(covered):
        best = int(np.lexsort((topics, -probs[i]))[0])
        is_other = threshold is not None and float(probs[i, best]) < threshold
        predicted = OTHER_TOPIC_ID if is_other else int(topics[best])
        correct[i] = float(predicted == label_ids[i])
        assigned[i] = 0.0 if is_other else 1.0
    return covered, correct, assigned


def mean_over(values: Matrix, mask: BoolArray) -> float:
    return float(values[mask].mean()) if bool(mask.any()) else math.nan


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float
    se: float
    n: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "estimate": _r(self.estimate),
            "low": _r(self.low),
            "high": _r(self.high),
            "se": _r(self.se),
            "n": self.n,
        }


def _r(x: float) -> float:
    return round(x, 6) if math.isfinite(x) else math.nan


def cluster_bootstrap(
    values: Matrix, mask: BoolArray, groups: IntArray, *, resamples: int, seed: int
) -> Interval:
    """Mean of `values` over `mask`, with a 95% percentile interval and a standard error.

    Whole groups are resampled with replacement, so Reels from one source count as the cluster
    they are. Fewer than two groups gives no interval (NaN), never a made-up one.
    """
    n = int(mask.sum())
    if n == 0:
        return Interval(math.nan, math.nan, math.nan, math.nan, 0)
    estimate = mean_over(values, mask)
    unique = np.unique(groups[mask])
    if len(unique) < 2:
        return Interval(estimate, math.nan, math.nan, math.nan, n)
    index = {g: j for j, g in enumerate(unique)}
    sums = np.zeros(len(unique))
    counts = np.zeros(len(unique))
    for i in np.flatnonzero(mask):
        j = index[groups[i]]
        sums[j] += values[i]
        counts[j] += 1
    draws = np.random.default_rng(seed).integers(0, len(unique), size=(resamples, len(unique)))
    stats = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    low, high = np.percentile(stats, [2.5, 97.5])
    return Interval(estimate, float(low), float(high), float(stats.std(ddof=1)), n)

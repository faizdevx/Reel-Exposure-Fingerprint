"""Compare text only, visual only and fused; tune weights, temperatures and the `other` threshold.

See `app.evaluation` for the rules. In short: tune on a development split only, confirm on a
held-out split once, prefer the spec defaults unless the data clearly says otherwise, and refuse to
recommend anything below the minimum data. Nothing is fitted: the "parameters" are the weights,
two temperatures and one threshold, chosen from a small grid.
"""

import itertools
import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from app.evaluation.cosines import CosineSet
from app.evaluation.metrics import (
    BoolArray,
    IntArray,
    Interval,
    Matrix,
    cluster_bootstrap,
    fuse_rows,
    other_accuracy,
    per_reel,
    softmax_rows,
)
from app.worker.fusion import OTHER_TOPIC_ID

REPORT_FORMAT = "fusion-evaluation/1"

# Below any of these the report still carries metrics and intervals but recommends nothing.
MIN_LABELLED = 200  # spec §10: hand-label 200 Reels
MIN_GROUPS = 10  # independent sources, so the split and the intervals mean something
MIN_BOTH = 100  # Reels that have text and frames, needed to compare and weigh modalities
MIN_TEST = 40  # held-out Reels with both modalities
MIN_OTHER = 20  # Reels labelled `other`, needed to set a threshold

TEST_FRACTION = 0.3
RESAMPLES = 1000

W_GRID = (0.2, 0.35, 0.5, 0.65, 0.8)  # weight of text; visual gets 1 - w
TAU_GRID = (0.005, 0.01, 0.02, 0.05)
THRESHOLD_GRID: tuple[float | None, ...] = (None, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7)

FALLBACK_TAU_TEXT = 0.01
FALLBACK_TAU_VISUAL = 0.02


@dataclass(frozen=True)
class Setting:
    w_text: float
    tau_text: float
    tau_visual: float
    other_threshold: float | None = None

    @property
    def w_visual(self) -> float:
        return round(1.0 - self.w_text, 10)


def default_setting(data: CosineSet) -> Setting:
    """The spec starting point: equal weights, the temperatures the pipelines ship with."""
    tau_text = data.meta.get("text_tau_default")
    tau_visual = data.meta.get("visual_tau_default")
    return Setting(
        0.5,
        float(tau_text) if isinstance(tau_text, int | float) else FALLBACK_TAU_TEXT,
        float(tau_visual) if isinstance(tau_visual, int | float) else FALLBACK_TAU_VISUAL,
    )


@dataclass(frozen=True)
class Probabilities:
    text: Matrix
    visual: Matrix
    fused: Matrix


def probabilities(data: CosineSet, setting: Setting) -> Probabilities:
    p_text = softmax_rows(data.cos_text, setting.tau_text)
    p_visual = softmax_rows(data.cos_visual, setting.tau_visual)
    return Probabilities(
        p_text, p_visual, fuse_rows(p_text, p_visual, setting.w_text, setting.w_visual)
    )


def group_codes(data: CosineSet) -> IntArray:
    codes = {g: i for i, g in enumerate(sorted(set(data.groups)))}
    return np.asarray([codes[g] for g in data.groups], dtype=np.int64)


def has_both(data: CosineSet) -> BoolArray:
    return (~np.isnan(data.cos_text).any(axis=1)) & (~np.isnan(data.cos_visual).any(axis=1))


def split_groups(groups: IntArray, test_fraction: float, seed: int) -> tuple[BoolArray, BoolArray]:
    """Whole groups go to the held-out part, in a seeded random order, until it is big enough."""
    unique = np.unique(groups)
    order = np.random.default_rng(seed).permutation(unique)
    target = test_fraction * len(groups)
    test_groups: list[int] = []
    taken = 0
    for g in order:
        if taken >= target or len(test_groups) >= len(unique) - 1:
            break
        test_groups.append(int(g))
        taken += int((groups == g).sum())
    test = np.isin(groups, test_groups)
    return ~test, test


# ------------------------------------------------------------------ metrics for one setting


def _metrics(
    probs: Matrix, data: CosineSet, groups: IntArray, mask: BoolArray, seed: int
) -> dict[str, Any]:
    pr = per_reel(probs, data.label_ids, data.topic_ids)
    use = pr.covered & mask
    kwargs = {"resamples": RESAMPLES, "seed": seed}
    return {
        "top1": cluster_bootstrap(pr.top1, use, groups, **kwargs).as_dict(),
        "top3": cluster_bootstrap(pr.top3, use, groups, **kwargs).as_dict(),
        "log_loss": cluster_bootstrap(pr.loss, use, groups, **kwargs).as_dict(),
    }


def compare_modes(
    data: CosineSet, setting: Setting, groups: IntArray, mask: BoolArray, seed: int
) -> dict[str, Any]:
    """Text only, visual only and fused at one setting.

    `paired` scores the three on the same Reels (those with text and frames): the fair comparison.
    `all_available` scores each on every Reel that has what it needs, which is what the product
    does, but the three then cover different Reels and must not be compared with each other.
    """
    p = probabilities(data, setting)
    both = has_both(data) & mask
    paired = {
        "n_reels": int((both & (data.label_ids != OTHER_TOPIC_ID)).sum()),
        "text_only": _metrics(p.text, data, groups, both, seed),
        "visual_only": _metrics(p.visual, data, groups, both, seed),
        "fused": _metrics(p.fused, data, groups, both, seed),
    }
    pr_t, pr_v, pr_f = (
        per_reel(x, data.label_ids, data.topic_ids) for x in (p.text, p.visual, p.fused)
    )
    use = pr_f.covered & pr_t.covered & pr_v.covered & both
    best_single = np.where(pr_t.loss <= pr_v.loss, pr_t.loss, pr_v.loss)
    paired["fused_minus_text_log_loss"] = cluster_bootstrap(
        pr_f.loss - pr_t.loss, use, groups, resamples=RESAMPLES, seed=seed
    ).as_dict()
    paired["fused_minus_visual_log_loss"] = cluster_bootstrap(
        pr_f.loss - pr_v.loss, use, groups, resamples=RESAMPLES, seed=seed
    ).as_dict()
    paired["fused_minus_best_single_per_reel_log_loss"] = cluster_bootstrap(
        pr_f.loss - best_single, use, groups, resamples=RESAMPLES, seed=seed
    ).as_dict()
    return {
        "setting": asdict(setting),
        "paired": paired,
        "all_available": {
            "text_only": {
                **_metrics(p.text, data, groups, mask, seed),
                "n_reels": _count(p.text, data, mask),
            },
            "visual_only": {
                **_metrics(p.visual, data, groups, mask, seed),
                "n_reels": _count(p.visual, data, mask),
            },
            "fused": {
                **_metrics(p.fused, data, groups, mask, seed),
                "n_reels": _count(p.fused, data, mask),
            },
        },
    }


def _count(probs: Matrix, data: CosineSet, mask: BoolArray) -> int:
    covered = per_reel(probs, data.label_ids, data.topic_ids).covered
    return int((covered & mask).sum())


# ------------------------------------------------------------------ tuning


def _grid(default: Setting) -> list[Setting]:
    return [Setting(w, tt, tv, None) for w, tt, tv in itertools.product(W_GRID, TAU_GRID, TAU_GRID)]


def _nearest(value: float, grid: tuple[float, ...]) -> int:
    return int(np.argmin([abs(math.log(value) - math.log(g)) if value > 0 else 0 for g in grid]))


def _distance(s: Setting, default: Setting) -> int:
    w0 = int(np.argmin([abs(default.w_text - w) for w in W_GRID]))
    return (
        abs(W_GRID.index(s.w_text) - w0)
        + abs(TAU_GRID.index(s.tau_text) - _nearest(default.tau_text, TAU_GRID))
        + abs(TAU_GRID.index(s.tau_visual) - _nearest(default.tau_visual, TAU_GRID))
    )


def tune_fusion(
    data: CosineSet, groups: IntArray, dev: BoolArray, default: Setting, seed: int
) -> dict[str, Any]:
    """Pick weights and temperatures by fused log loss on development Reels with both modalities.

    The best grid point is not taken as it is: every point within one standard error of it counts
    as equally good, and the one nearest the defaults is chosen.
    """
    use_mask = has_both(data) & dev
    table: list[tuple[Setting, float]] = []
    losses: dict[Setting, Matrix] = {}
    for setting in _grid(default):
        pr = per_reel(probabilities(data, setting).fused, data.label_ids, data.topic_ids)
        covered = pr.covered & use_mask
        table.append((setting, float(pr.loss[covered].mean())))
        losses[setting] = pr.loss
    best, best_loss = min(table, key=lambda t: t[1])
    covered = (
        per_reel(probabilities(data, best).fused, data.label_ids, data.topic_ids).covered & use_mask
    )
    se = cluster_bootstrap(losses[best], covered, groups, resamples=RESAMPLES, seed=seed).se
    cutoff = best_loss + (se if math.isfinite(se) else 0.0)
    within = [(s, loss) for s, loss in table if loss <= cutoff]
    chosen, chosen_loss = min(
        within,
        key=lambda t: (_distance(t[0], default), t[1], t[0].w_text, t[0].tau_text, t[0].tau_visual),
    )
    ranked = sorted(table, key=lambda t: t[1])[:10]
    return {
        "development_reels": int(covered.sum()),
        "grid_points": len(table),
        "best": {"setting": asdict(best), "log_loss": round(best_loss, 6), "se": round(se, 6)},
        "rule": "nearest to the defaults among points within one standard error of the best",
        "points_within_one_se": len(within),
        "selected": {"setting": asdict(chosen), "log_loss": round(chosen_loss, 6)},
        "ten_best": [{"setting": asdict(s), "log_loss": round(v, 6)} for s, v in ranked],
    }


def tune_threshold(
    data: CosineSet, groups: IntArray, dev: BoolArray, setting: Setting, seed: int
) -> dict[str, Any]:
    """Pick the `other` threshold by accuracy with `other` as a class, on the development Reels.

    `None` (never `other`) is the default. A threshold is chosen only if it is better than that by
    more than one standard error; among thresholds within one standard error of the best, the
    smallest wins, because it assigns `other` least often.
    """
    fused = probabilities(data, setting).fused
    rows: list[tuple[float | None, float, float]] = []
    per_threshold: dict[float | None, Matrix] = {}
    mask: BoolArray = dev
    for threshold in THRESHOLD_GRID:
        covered, correct, assigned = other_accuracy(
            fused, data.label_ids, data.topic_ids, threshold
        )
        use = covered & dev
        mask = use
        per_threshold[threshold] = correct
        rows.append((threshold, float(correct[use].mean()), float(assigned[use].mean())))
    best_threshold, best_acc, _ = max(rows, key=lambda r: r[1])
    se = cluster_bootstrap(
        per_threshold[best_threshold], mask, groups, resamples=RESAMPLES, seed=seed
    ).se
    cutoff = best_acc - (se if math.isfinite(se) else 0.0)
    within = [r for r in rows if r[1] >= cutoff]
    chosen = min(within, key=lambda r: -1.0 if r[0] is None else r[0])
    return {
        "development_reels": int(mask.sum()),
        "best": {
            "threshold": best_threshold,
            "accuracy_with_other": round(best_acc, 6),
            "se": round(se, 6),
        },
        "selected": {"threshold": chosen[0], "accuracy_with_other": round(chosen[1], 6)},
        "curve": [
            {
                "threshold": t,
                "accuracy_with_other": round(a, 6),
                "share_assigned_to_topic": round(c, 6),
            }
            for t, a, c in rows
        ],
    }


# ------------------------------------------------------------------ the whole evaluation


def sufficiency(data: CosineSet, dev: BoolArray, test: BoolArray) -> dict[str, Any]:
    n = len(data.ids)
    topic_labelled = data.label_ids != OTHER_TOPIC_ID
    both = has_both(data)
    checks = {
        "labelled_reels": (n, MIN_LABELLED),
        "groups": (len(set(data.groups)), MIN_GROUPS),
        "reels_with_text_and_frames": (int((both & topic_labelled).sum()), MIN_BOTH),
        "held_out_reels_with_text_and_frames": (
            int((both & topic_labelled & test).sum()),
            MIN_TEST,
        ),
        "development_reels_with_text_and_frames": (
            int((both & topic_labelled & dev).sum()),
            MIN_TEST,
        ),
    }
    failed = [name for name, (have, need) in checks.items() if have < need]
    return {
        "checks": {name: {"have": have, "need": need} for name, (have, need) in checks.items()},
        "enough_to_tune_fusion": not failed,
        "failed": failed,
        "reels_labelled_other": int((data.label_ids == OTHER_TOPIC_ID).sum()),
        "enough_to_tune_threshold": (
            not failed and int((data.label_ids == OTHER_TOPIC_ID).sum()) >= MIN_OTHER
        ),
    }


def evaluate(data: CosineSet, *, seed: int) -> dict[str, Any]:
    groups = group_codes(data)
    everyone = np.ones(len(data.ids), dtype=bool)
    dev, test = split_groups(groups, TEST_FRACTION, seed)
    default = default_setting(data)
    enough = sufficiency(data, dev, test)

    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "seed": seed,
        "inputs": {
            "reels": len(data.ids),
            "groups": len(set(data.groups)),
            "labels_sha256": data.meta.get("labels_sha256"),
            "text_pipeline_version": data.meta.get("text_pipeline_version"),
            "visual": data.meta.get("visual"),
            "reels_with_text": int((~np.isnan(data.cos_text).any(axis=1)).sum()),
            "reels_with_frames": int((~np.isnan(data.cos_visual).any(axis=1)).sum()),
            "reels_with_both": int(has_both(data).sum()),
        },
        "split": {
            "test_fraction": TEST_FRACTION,
            "development_reels": int(dev.sum()),
            "held_out_reels": int(test.sum()),
        },
        "sufficiency": enough,
        # Scored on all Reels at the defaults. For reading, never used to choose anything.
        "defaults_on_all_reels": compare_modes(data, default, groups, everyone, seed),
    }

    if not enough["enough_to_tune_fusion"]:
        report["tuning"] = None
        report["held_out"] = None
        report["recommendation"] = {
            "status": "insufficient_data",
            "reasons": [
                f"{name}: have {enough['checks'][name]['have']}, "
                f"need {enough['checks'][name]['need']}"
                for name in enough["failed"]
            ],
            "settings": None,
            "note": "Nothing was tuned. Keep the pipeline defaults; collect more labelled Reels.",
        }
        return report

    tuning = tune_fusion(data, groups, dev, default, seed)
    selected = Setting(**tuning["selected"]["setting"])
    report["tuning"] = tuning

    held_default = compare_modes(data, default, groups, test, seed)
    held_selected = compare_modes(data, selected, groups, test, seed)
    # Selected minus defaults, fused log loss per Reel, on the held-out Reels. Negative is better.
    pr_d = per_reel(probabilities(data, default).fused, data.label_ids, data.topic_ids)
    pr_s = per_reel(probabilities(data, selected).fused, data.label_ids, data.topic_ids)
    held_mask = has_both(data) & test & pr_d.covered & pr_s.covered
    diff = cluster_bootstrap(
        pr_s.loss - pr_d.loss, held_mask, groups, resamples=RESAMPLES, seed=seed
    )
    confirmed = selected == default or (math.isfinite(diff.high) and diff.high < 0.0)
    report["held_out"] = {
        "defaults": held_default,
        "selected": held_selected,
        "selected_minus_defaults_fused_log_loss": diff.as_dict(),
        "confirmed": confirmed,
    }

    final = selected if confirmed else default
    reasons: list[str] = []
    if not confirmed:
        reasons.append(
            "the tuned setting was not better than the defaults on the held-out Reels "
            "(the interval of the difference includes 0): keeping the defaults"
        )
    settings: dict[str, Any] = asdict(final)

    if enough["enough_to_tune_threshold"]:
        threshold_dev = tune_threshold(data, groups, dev, final, seed)
        chosen = threshold_dev["selected"]["threshold"]
        fused = probabilities(data, final).fused
        _, c_none, _ = other_accuracy(fused, data.label_ids, data.topic_ids, None)
        cov, c_thr, _ = other_accuracy(fused, data.label_ids, data.topic_ids, chosen)
        gain = cluster_bootstrap(c_thr - c_none, cov & test, groups, resamples=RESAMPLES, seed=seed)
        threshold_ok = chosen is None or (math.isfinite(gain.low) and gain.low > 0.0)
        report["other_threshold"] = {
            "development": threshold_dev,
            "held_out_gain_in_accuracy_with_other": gain.as_dict(),
            "confirmed": threshold_ok,
        }
        settings["other_threshold"] = chosen if threshold_ok else None
        if not threshold_ok:
            reasons.append(
                "no `other` threshold beat never assigning `other` on the held-out Reels: "
                "leaving the threshold unset"
            )
    else:
        report["other_threshold"] = None
        settings["other_threshold"] = None
        reasons.append(
            f"fewer than {MIN_OTHER} Reels are labelled `other`: the threshold is left unset"
        )

    report["recommendation"] = {
        "status": "recommended",
        "reasons": reasons,
        "settings": settings,
        "note": (
            "Settings are from the grid and were checked once on held-out Reels. Put them in "
            "app.worker.config / vision_config (a new pipeline_version) and re-run the evaluation."
        ),
    }
    return report


__all__ = ["Interval", "Setting", "evaluate", "split_groups"]

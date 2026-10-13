"""Scoring the predictions and comparing the models (spec §10, M6).

Every figure here is computed from predictions that were made before their Reel (`engine.replay`).

- Top-1: the model's most likely topic was the observed one. Top-3: it was among the three most
  likely. Log loss: `-ln` of the probability the model gave the observed topic.
- All-time and rolling (the last `rolling_window` Reels) figures per model.
- Comparisons between the hybrid and each other model (log loss and top-1, per Reel differences
  with a 95% cluster bootstrap interval over sessions), **only once `min_events_for_conclusion`
  Reels have been scored**. Before that the summary says the evaluation is not complete and states
  no comparison and no verdict, so nothing can be read as a success.
- A verdict is a plain statement about the interval: lower, higher, or no clear difference. The
  hybrid "beats the required baselines" only when its log loss is clearly lower than both the
  most-frequent and the decayed-fingerprint baselines.
"""

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from app.evaluation.metrics import cluster_bootstrap
from app.prediction.config import MODELS, PredictionConfig
from app.prediction.engine import EventPrediction, log_loss, ranked

EVALUATION_FORMAT = "prediction-evaluation/1"


def _rounded(value: float) -> float | None:
    return round(float(value), 6) if math.isfinite(value) else None


def _clean(value: Any) -> Any:
    """Replace NaN and infinity by None, so the summary is strict JSON (jsonb refuses NaN)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def scores_of(event: EventPrediction, model: str) -> tuple[float, float, float]:
    """`(top1, top3, log loss)` of one model for one Reel."""
    order = ranked(event.models[model])
    top1 = 1.0 if order[0] == event.actual_topic else 0.0
    top3 = 1.0 if event.actual_topic in order[:3] else 0.0
    return top1, top3, log_loss(event.models[model], event.actual_topic)


def _arrays(events: Sequence[EventPrediction]) -> dict[str, dict[str, np.ndarray[Any, Any]]]:
    out: dict[str, dict[str, np.ndarray[Any, Any]]] = {}
    for model in MODELS:
        rows = [scores_of(e, model) for e in events]
        out[model] = {
            "top1": np.array([r[0] for r in rows]),
            "top3": np.array([r[1] for r in rows]),
            "loss": np.array([r[2] for r in rows]),
        }
    return out


def _figures(arrays: dict[str, np.ndarray[Any, Any]], start: int = 0) -> dict[str, Any]:
    n = len(arrays["loss"]) - start
    if n <= 0:
        return {"n": 0, "top1": None, "top3": None, "log_loss": None}
    return {
        "n": n,
        "top1": _rounded(arrays["top1"][start:].mean()),
        "top3": _rounded(arrays["top3"][start:].mean()),
        "log_loss": _rounded(arrays["loss"][start:].mean()),
    }


def _verdict(low: float, high: float) -> str:
    if not (math.isfinite(low) and math.isfinite(high)):
        return "no_interval"
    if high < 0.0:
        return "hybrid_lower"
    if low > 0.0:
        return "hybrid_higher"
    return "no_clear_difference"


def summarize(events: Sequence[EventPrediction], config: PredictionConfig) -> dict[str, Any]:
    n = len(events)
    arrays = _arrays(events)
    window_start = max(0, n - config.rolling_window)
    complete = n >= config.min_events_for_conclusion
    summary: dict[str, Any] = {
        "format": EVALUATION_FORMAT,
        "scored_reels": n,
        "required_reels": config.min_events_for_conclusion,
        "complete": complete,
        "status": (
            "complete"
            if complete
            else f"not complete: {n} of {config.min_events_for_conclusion} Reels scored"
        ),
        "rolling_window": config.rolling_window,
        "models": {
            model: {
                "all_time": _figures(arrays[model]),
                "rolling": _figures(arrays[model], window_start),
            }
            for model in MODELS
        },
        "comparisons": None,
        "hybrid_beats_required_baselines": None,
    }
    if not complete:
        return _clean(summary)  # type: ignore[no-any-return]

    session_ids = sorted({str(e.session_id) for e in events})
    index = {s: i for i, s in enumerate(session_ids)}
    groups = np.array([index[str(e.session_id)] for e in events], dtype=np.int64)
    everyone = np.ones(n, dtype=bool)
    comparisons: dict[str, Any] = {}
    for other in MODELS:
        if other == "hybrid":
            continue
        loss_diff = arrays["hybrid"]["loss"] - arrays[other]["loss"]
        top1_diff = arrays["hybrid"]["top1"] - arrays[other]["top1"]
        kwargs = {"resamples": config.bootstrap_resamples, "seed": config.bootstrap_seed}
        loss_ci = cluster_bootstrap(loss_diff, everyone, groups, **kwargs)
        top1_ci = cluster_bootstrap(top1_diff, everyone, groups, **kwargs)
        comparisons[other] = {
            "log_loss_difference": loss_ci.as_dict(),
            "top1_difference": top1_ci.as_dict(),
            "log_loss_verdict": _verdict(loss_ci.low, loss_ci.high),
        }
    summary["comparisons"] = comparisons
    summary["hybrid_beats_required_baselines"] = all(
        comparisons[b]["log_loss_verdict"] == "hybrid_lower"
        for b in ("most_frequent", "decayed_fingerprint")
    )
    return _clean(summary)  # type: ignore[no-any-return]

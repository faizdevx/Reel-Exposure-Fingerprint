"""Latency statistics. Milliseconds in, plain dicts out (strict JSON)."""

import math
import statistics
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Any


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the usual definition, q in 0..100)."""
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * q / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    fraction = rank - low
    return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction)


def summarize(values_ms: Sequence[float]) -> dict[str, Any]:
    """n, min, mean, p50, p90, p95, p99, max. Empty input gives `n: 0` and nulls."""
    values = sorted(float(v) for v in values_ms if math.isfinite(v))
    if not values:
        return {
            "n": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
        }

    def r(x: float) -> float:
        return round(x, 3)

    return {
        "n": len(values),
        "min": r(values[0]),
        "mean": r(statistics.fmean(values)),
        "p50": r(percentile(values, 50)),
        "p90": r(percentile(values, 90)),
        "p95": r(percentile(values, 95)),
        "p99": r(percentile(values, 99)),
        "max": r(values[-1]),
    }


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    """Run `fn`; return its result and the wall time in milliseconds."""
    start = perf_counter()
    result = fn()
    return result, (perf_counter() - start) * 1000.0


def repeat_ms(fn: Callable[[], Any], n: int, warmup: int = 2) -> list[float]:
    """Wall time of `n` calls after `warmup` calls that are not counted."""
    for _ in range(warmup):
        fn()
    return [timed(fn)[1] for _ in range(n)]

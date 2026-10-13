"""The four evaluations of spec §10, computed from stored data. Nothing here is tuned to look good.

1. Topic labelling: text only, visual only and fused, top-1 and top-3, on the hand-labelled set
   (`app.evaluation.tuning.compare_modes`). Needs the labelled set; without it the section says so.
2. Fingerprint against self-report: Spearman correlation between a user's 1-5 topic ratings and
   each fingerprint (exposure, engagement), per user.
3. Prediction: the five models, prequentially, top-1, top-3 and log loss.
4. Fingerprint stability: the same Reels replayed in normal order, in shuffled session order, and
   with half-life 3, 7 and 14 days; does the order of the top five topics change?

Every function is pure (data in, plain dict out) so it can be tested without a database. A result
is reported whichever way it falls. Where the data is below the spec's minimum, the result is still
computed, and marked with the minimum it misses: it is then a description, not a finding.
"""

import math
import random
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from app.fingerprint.engine import (
    FingerprintConfig,
    Observation,
    order_key,
    rebuild,
)
from app.prediction.config import MODELS, PredictionConfig
from app.prediction.engine import EventPrediction, replay
from app.prediction.evaluation import scores_of, summarize

REPORT_FORMAT = "evaluation-report/1"

#: Spec §10: "Minimum data before any claim: 500 Reels per user for fingerprints".
MIN_REELS_FINGERPRINT = 500
HALF_LIVES_DAYS = (3.0, 7.0, 14.0)
BASELINE_HALF_LIFE_DAYS = 7.0
SHUFFLES = 20
TOP_N = 5
#: The assignment version name used in snapshots made only for evaluation (never stored).
EVAL_ASSIGNMENT_VERSION = "evaluation"


# ------------------------------------------------------------------ small statistics


def average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks from 1, tied values sharing the mean of their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n != len(y) or n < 2:
        return None
    mx, my = math.fsum(x) / n, math.fsum(y) / n
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    if sxx <= 0.0 or syy <= 0.0:
        return None  # one side is constant: the correlation is not defined
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's rho: Pearson on average ranks, so ties are handled. None when undefined."""
    if len(x) != len(y) or len(x) < 2:
        return None
    return pearson(average_ranks(x), average_ranks(y))


def kendall_tau(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Kendall's tau-b between two score lists of the same items."""
    n = len(x)
    if n != len(y) or n < 2:
        return None
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx, dy = x[i] - x[j], y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    if denominator == 0.0:
        return None
    return (concordant - discordant) / denominator


def clean(value: Any) -> Any:
    """Replace NaN and infinity by None everywhere: the report is strict JSON, and a missing figure
    must read as missing, never as a number."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    return value


def _r(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, 6)


def top_topics(share: Mapping[int, float], n: int = TOP_N) -> list[int]:
    """The `n` topics with the largest share (ties to the lower id). Topics with no weight are
    not ranked: a topic the fingerprint never saw is not "fifth"."""
    present = [(k, v) for k, v in share.items() if v > 0.0]
    present.sort(key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in present[:n]]


# ------------------------------------------------------------------ 1. topic labelling


def labelling_section(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pull the six figures out of an `app.evaluation evaluate` report (or say there is none).

    Only the paired comparison counts as a comparison: the three modes scored on the same Reels,
    those that have text and frames. Their pass bars are spec §10: 70% top-1 and 85% top-3, fused.
    """
    if report is None:
        return {
            "status": "not_run",
            "reason": (
                "No hand-labelled set was given. The 200-Reel labelled set does not exist yet, "
                "so no top-1 or top-3 figure can be stated."
            ),
            "figures": None,
            "pass_bars": {"top1": 0.70, "top3": 0.85},
            "passed": None,
        }
    paired = clean(report["defaults_on_all_reels"]["paired"])
    sufficiency = report["sufficiency"]
    figures = {
        mode: {
            "top1": paired[mode]["top1"],
            "top3": paired[mode]["top3"],
            "log_loss": paired[mode]["log_loss"],
        }
        for mode in ("text_only", "visual_only", "fused")
    }
    fused = paired["fused"]
    top1, top3 = fused["top1"]["estimate"], fused["top3"]["estimate"]
    n_labelled = report["inputs"]["reels"]
    enough = n_labelled >= 200
    passed: bool | None = None
    if enough and paired["n_reels"] > 0 and top1 is not None and top3 is not None:
        passed = bool(top1 >= 0.70 and top3 >= 0.85)
    return {
        "status": "complete" if enough else "below_minimum",
        "reason": None
        if enough
        else f"only {n_labelled} labelled Reels; the spec asks for 200. Figures are descriptive.",
        "figures": figures,
        "paired_reels": paired["n_reels"],
        "labelled_reels": n_labelled,
        "inputs": clean(report["inputs"]),
        "sufficiency_failed": sufficiency.get("failed", []),
        "pass_bars": {"top1": 0.70, "top3": 0.85},
        "passed": passed,
    }


# ------------------------------------------------------------------ 2. self-report


@dataclass(frozen=True)
class SelfReportUser:
    user_id: uuid.UUID
    #: topic id -> rating 1..5. Empty when the survey was skipped or not answered.
    ratings: Mapping[int, int]
    survey_completed_at: datetime | None
    observations: Sequence[Observation]


def final_shares(
    observations: Sequence[Observation], config: FingerprintConfig
) -> tuple[dict[str, dict[int, float]], dict[str, int]]:
    result = rebuild(observations, config, EVAL_ASSIGNMENT_VERSION)
    return (
        {kind: state.share() for kind, state in result.states.items()},
        {kind: state.n_reels for kind, state in result.states.items()},
    )


def _correlate(
    ratings: Mapping[int, int], share: Mapping[int, float], topics: Sequence[int]
) -> float | None:
    """Rho between the ratings and the shares over the rated topics. A topic missing from the
    fingerprint has share 0: it was never seen, so it ranks last, not "unknown"."""
    return spearman([float(ratings[t]) for t in topics], [share.get(t, 0.0) for t in topics])


def self_report_section(
    users: Sequence[SelfReportUser], config: FingerprintConfig
) -> dict[str, Any]:
    """Spearman correlation of each user's ratings with their exposure and engagement fingerprint.

    Two readings per user: `at_survey` uses only Reels observed before the survey was completed
    (the spec's design: ratings given before the dashboard shows anything), `all_data` uses every
    Reel. Averages are of the per-user correlations, skipping users where one is undefined.
    """
    rows: list[dict[str, Any]] = []
    for user in users:
        topics = sorted(user.ratings)
        base: dict[str, Any] = {"user": str(user.user_id), "rated_topics": len(topics)}
        if not topics:
            rows.append({**base, "status": "no_ratings"})
            continue
        row: dict[str, Any] = {**base, "status": "rated"}
        views = {"all_data": list(user.observations)}
        if user.survey_completed_at is not None:
            views["at_survey"] = [
                o for o in user.observations if o.observed_at <= user.survey_completed_at
            ]
        for view, observations in views.items():
            shares, counts = final_shares(observations, config)
            row[view] = {
                "reels": counts["exposure"],
                "below_minimum": counts["exposure"] < MIN_REELS_FINGERPRINT,
                "exposure": _r(_correlate(user.ratings, shares["exposure"], topics)),
                "engagement": _r(_correlate(user.ratings, shares["engagement"], topics)),
            }
        rows.append(row)

    def summary(view: str) -> dict[str, Any]:
        usable = [r[view] for r in rows if r.get("status") == "rated" and view in r]
        out: dict[str, Any] = {"users": len(usable)}
        for kind in ("exposure", "engagement"):
            values = [u[kind] for u in usable if u[kind] is not None]
            out[kind] = {
                "n": len(values),
                "mean": _r(math.fsum(values) / len(values)) if values else None,
            }
        paired = [
            u["engagement"] - u["exposure"]
            for u in usable
            if u["engagement"] is not None and u["exposure"] is not None
        ]
        out["engagement_minus_exposure"] = {
            "n": len(paired),
            "mean": _r(math.fsum(paired) / len(paired)) if paired else None,
            "users_engagement_higher": sum(1 for d in paired if d > 0),
            "users_exposure_higher": sum(1 for d in paired if d < 0),
            "users_equal": sum(1 for d in paired if d == 0),
        }
        out["users_below_500_reels"] = sum(1 for u in usable if u["below_minimum"])
        return out

    answered = [r for r in rows if r.get("status") == "rated"]
    return {
        "users_total": len(users),
        "users_with_ratings": len(answered),
        "expectation": "engagement correlates more than exposure; if not, that is a finding too",
        "min_reels_per_user": MIN_REELS_FINGERPRINT,
        "per_user": rows,
        "at_survey": summary("at_survey"),
        "all_data": summary("all_data"),
        "status": "not_run" if not answered else "computed",
    }


# ------------------------------------------------------------------ 3. prediction


def prediction_section(
    users: Mapping[str, Sequence[EventPrediction]], config: PredictionConfig
) -> dict[str, Any]:
    """Per user (`summarize`: a verdict only from 1,500 scored Reels) and pooled means over every
    scored Reel. Pooled means mix users with different amounts of data: they describe, they do not
    conclude."""
    per_user = {user: summarize(events, config) for user, events in users.items()}
    pooled_events = [e for events in users.values() for e in events]
    pooled: dict[str, Any] = {}
    for model in MODELS:
        scores = [scores_of(e, model) for e in pooled_events]
        n = len(scores)
        pooled[model] = {
            "n": n,
            "top1": _r(math.fsum(s[0] for s in scores) / n) if n else None,
            "top3": _r(math.fsum(s[1] for s in scores) / n) if n else None,
            "log_loss": _r(math.fsum(s[2] for s in scores) / n) if n else None,
        }
    complete_users = [u for u, s in per_user.items() if s["complete"]]
    return {
        "required_reels_per_user": config.min_events_for_conclusion,
        "users": len(users),
        "users_complete": len(complete_users),
        "scored_reels": len(pooled_events),
        "pooled": pooled,
        "per_user": per_user,
        "status": (
            "no_data"
            if not pooled_events
            else "complete"
            if len(complete_users) == len(users)
            else "not_complete"
        ),
    }


def replay_user(
    observations: Sequence[Observation],
    universe: Sequence[int],
    config: PredictionConfig,
    fp_config: FingerprintConfig,
) -> list[EventPrediction]:
    return replay(observations, universe, config, fp_config).events


# ------------------------------------------------------------------ 4. stability


def shuffle_sessions(observations: Sequence[Observation], rng: random.Random) -> list[Observation]:
    """The same Reels with the sessions in a random order.

    Inside a session the Reels keep their order and spacing. The sessions are laid out one after
    another in the new order, separated by the gaps the user's own history had, in the order those
    gaps occurred, so total time and the size of the gaps are unchanged. A Reel is never moved
    across sessions.
    """
    by_session: dict[uuid.UUID, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_session[obs.session_id].append(obs)
    sessions = sorted(
        (sorted(group, key=order_key) for group in by_session.values()),
        key=lambda g: order_key(g[0]),
    )
    if len(sessions) < 2:
        return list(observations)
    gaps = [
        max(timedelta(0), sessions[i + 1][0].observed_at - sessions[i][-1].observed_at)
        for i in range(len(sessions) - 1)
    ]
    shuffled = sessions[:]
    rng.shuffle(shuffled)
    out: list[Observation] = []
    cursor = sessions[0][0].observed_at
    for position, group in enumerate(shuffled):
        start = group[0].observed_at
        out.extend(replace(o, observed_at=cursor + (o.observed_at - start)) for o in group)
        cursor += group[-1].observed_at - start
        if position < len(gaps):
            cursor += gaps[position]
    return out


def _shares(
    observations: Sequence[Observation], config: FingerprintConfig
) -> dict[str, dict[int, float]]:
    return final_shares(observations, config)[0]


def _compare(
    kind_shares: Mapping[int, float], baseline: Mapping[int, float], topics: Sequence[int]
) -> dict[str, Any]:
    top, base_top = top_topics(kind_shares), top_topics(baseline)
    tau = kendall_tau(
        [kind_shares.get(t, 0.0) for t in topics], [baseline.get(t, 0.0) for t in topics]
    )
    return {
        "top5": top,
        "same_top5_set": set(top) == set(base_top),
        "same_top5_order": top == base_top,
        "kendall_tau_all_topics": _r(tau),
        "max_share_difference": _r(
            max((abs(kind_shares.get(t, 0.0) - baseline.get(t, 0.0)) for t in topics), default=0.0)
        ),
    }


def stability_user(
    observations: Sequence[Observation],
    topics: Sequence[int],
    config: FingerprintConfig,
    *,
    seed: int,
    shuffles: int = SHUFFLES,
) -> dict[str, Any]:
    """Replay one user's Reels in the variants and compare each with normal order, half-life 7.

    `normal order` at half-life 7 is the baseline, so it compares with itself and is not listed.
    """
    base_config = replace(config, half_life_days=BASELINE_HALF_LIFE_DAYS)
    baseline = _shares(observations, base_config)
    result: dict[str, Any] = {
        "reels": len(observations),
        "below_minimum": len(observations) < MIN_REELS_FINGERPRINT,
        "baseline_top5": {k: top_topics(v) for k, v in baseline.items()},
        "half_life": {},
        "shuffled_session_order": {},
    }
    for half_life in HALF_LIVES_DAYS:
        shares = _shares(observations, replace(config, half_life_days=half_life))
        result["half_life"][f"{half_life:g}"] = {
            kind: _compare(shares[kind], baseline[kind], topics) for kind in shares
        }
    rng = random.Random(seed)  # noqa: S311  (a reproducible shuffle, not security)
    sessions = len({o.session_id for o in observations})
    runs: dict[str, list[dict[str, Any]]] = {kind: [] for kind in baseline}
    if sessions >= 2:
        for _ in range(shuffles):
            shares = _shares(shuffle_sessions(observations, rng), base_config)
            for kind in shares:
                runs[kind].append(_compare(shares[kind], baseline[kind], topics))
    for kind, compared in runs.items():
        taus = [
            c["kendall_tau_all_topics"] for c in compared if c["kendall_tau_all_topics"] is not None
        ]
        result["shuffled_session_order"][kind] = {
            "sessions": sessions,
            "shuffles": len(compared),
            "same_top5_order": sum(1 for c in compared if c["same_top5_order"]),
            "same_top5_set": sum(1 for c in compared if c["same_top5_set"]),
            "kendall_tau_min": _r(min(taus)) if taus else None,
            "kendall_tau_mean": _r(math.fsum(taus) / len(taus)) if taus else None,
        }
    return result


def stability_section(
    users: Mapping[str, Sequence[Observation]],
    topics: Sequence[int],
    config: FingerprintConfig,
    *,
    seed: int,
) -> dict[str, Any]:
    per_user = {user: stability_user(obs, topics, config, seed=seed) for user, obs in users.items()}
    return {
        "pass_bar": "the order of the top 5 topics should not flip on small parameter changes",
        "baseline": "normal order, half-life 7 days",
        "variants": [
            "shuffled session order",
            "half-life 3 days",
            "half-life 7 days",
            "half-life 14 days",
        ],
        "shuffles": SHUFFLES,
        "min_reels_per_user": MIN_REELS_FINGERPRINT,
        "per_user": per_user,
        "status": "no_data" if not per_user else "computed",
    }


# ------------------------------------------------------------------ the whole report


def build_report(
    *,
    labelling: Mapping[str, Any] | None,
    self_report: Sequence[SelfReportUser],
    predictions: Mapping[str, Sequence[EventPrediction]],
    stability_inputs: Mapping[str, Sequence[Observation]],
    topics: Sequence[int],
    fp_config: FingerprintConfig,
    prediction_config: PredictionConfig,
    seed: int,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "seed": seed,
        "meta": dict(meta),
        "labelling": labelling_section(labelling),
        "self_report": self_report_section(self_report, fp_config),
        "prediction": prediction_section(predictions, prediction_config),
        "stability": stability_section(stability_inputs, topics, fp_config, seed=seed),
    }
    return clean(report)  # type: ignore[no-any-return]


__all__ = [
    "SelfReportUser",
    "build_report",
    "kendall_tau",
    "labelling_section",
    "prediction_section",
    "replay_user",
    "self_report_section",
    "shuffle_sessions",
    "spearman",
    "stability_section",
    "top_topics",
]

"""Pure fingerprint maths. No database, no clock, no randomness: same input, same bits.

Rules (spec §5.2):

- Exposure gain `g = 1`.
- Engagement gain `g = clip(dwell / max(duration, 5 s), 0.1, 2.0)`; a dwell under 1.5 s is a skip
  and gets `g = 0.1`. A dwell of exactly 1.5 s is not a skip.
- Half-life `h` starts at 7 days. `dt` is the difference of two `observed_at` values, never of
  processing times (CONTRIBUTING.md, ADR-011).

Missing data is not filled in:

- A Reel with no topic distribution (no usable text and no frames, so no assignment) is skipped by
  both fingerprints.
- A Reel with no dwell is skipped by the engagement fingerprint only, because its gain is
  undefined. The exposure fingerprint does not use dwell.
- A skipped Reel changes nothing, including the state's last update time. Decay composes
  (`2^-a * 2^-b = 2^-(a+b)`), so the next applied Reel decays over the whole gap.
- A missing video duration is the one case the spec formula answers itself: `max(duration, 5 s)`
  with an unknown duration is the 5 s floor. OPEN: confirm with the owner that this is wanted. A
  Reel longer than 5 s then scores a higher gain than it would with its real duration.

Modalities: `probabilities` is the Reel's fused distribution. How it was made (text only today,
text and visual after M4) is not this module's concern; the same update runs for any of them.

Known float behaviour: everything is IEEE-754 double precision. `math.pow` is the platform `pow`;
results are identical on one machine and library build, and may differ in the last bit between
platforms. Sums use `math.fsum`. Iteration order is sorted by topic id.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

FingerprintKind = Literal["exposure", "engagement"]
KINDS: tuple[FingerprintKind, ...] = ("exposure", "engagement")

FINGERPRINT_SCHEMA = "fingerprint/1"
WEIGHTS_SCHEMA = "fp-weights/1"
SECONDS_PER_DAY = 86400.0
_PROB_SUM_TOLERANCE = 1e-6


class FingerprintError(Exception):
    """Input the engine cannot use."""


class OutOfOrderError(FingerprintError):
    """An observation older than the state's last update. The caller must replay (ADR-011)."""


@dataclass(frozen=True)
class FingerprintConfig:
    half_life_days: float = 7.0
    min_gain: float = 0.1
    max_gain: float = 2.0
    #: Floor of the duration in the engagement ratio: `max(duration, 5 s)`.
    min_duration_ms: int = 5000
    #: A dwell strictly under this is a skip.
    skip_dwell_ms: int = 1500
    skip_gain: float = 0.1

    def __post_init__(self) -> None:
        if not (math.isfinite(self.half_life_days) and self.half_life_days > 0.0):
            raise ValueError("half_life_days must be positive and finite")
        if not (math.isfinite(self.min_gain) and math.isfinite(self.max_gain)):
            raise ValueError("gain bounds must be finite")
        if not 0.0 < self.min_gain <= self.max_gain:
            raise ValueError("need 0 < min_gain <= max_gain")
        if self.min_duration_ms <= 0 or self.skip_dwell_ms < 0:
            raise ValueError("min_duration_ms must be positive and skip_dwell_ms non-negative")
        if not (math.isfinite(self.skip_gain) and self.skip_gain >= 0.0):
            raise ValueError("skip_gain must be finite and non-negative")


def canonical_fingerprint_config(
    config: FingerprintConfig, assignment_version: str
) -> dict[str, Any]:
    return {
        "schema": FINGERPRINT_SCHEMA,
        "update": "W_k = W_k * 2^(-dt/h) + g * p(k)",
        "display": "W_k / sum(W)",
        "dt": "observed_at difference, in days",
        # The topic assignments the weights were built from (models, taxonomy, tau). Their
        # version covers everything upstream, so one string pins the whole chain (N9).
        "assignment_pipeline_version": assignment_version,
        "params": asdict(config),
    }


def fingerprint_version(config: FingerprintConfig, assignment_version: str) -> str:
    """`pipeline_version` stored on snapshots. Changes with any parameter or upstream version.

    ADR-008 (Proposed) would split this from the assignment version. Until it is accepted this one
    string carries both, as N9 requires.
    """
    blob = json.dumps(
        canonical_fingerprint_config(config, assignment_version),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"fp1-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


# ---- gains


def exposure_gain() -> float:
    return 1.0


def engagement_gain(
    dwell_ms: int | None, video_duration_ms: int | None, config: FingerprintConfig
) -> float | None:
    """Gain for the engagement fingerprint, or None when the dwell is unknown."""
    if dwell_ms is None:
        return None
    if dwell_ms < 0:
        raise FingerprintError("dwell_ms is negative")
    if video_duration_ms is not None and video_duration_ms < 0:
        raise FingerprintError("video_duration_ms is negative")
    if dwell_ms < config.skip_dwell_ms:
        return config.skip_gain
    denominator = max(video_duration_ms or 0, config.min_duration_ms)
    return min(config.max_gain, max(config.min_gain, dwell_ms / denominator))


def decay_factor(dt_seconds: float, config: FingerprintConfig) -> float:
    """`2^(-dt/h)`, for `dt >= 0`."""
    if dt_seconds < 0.0:
        raise OutOfOrderError("negative time step")
    return math.pow(2.0, -(dt_seconds / SECONDS_PER_DAY) / config.half_life_days)


# ---- state


@dataclass(frozen=True)
class FingerprintState:
    kind: FingerprintKind
    #: topic_id -> weight. Treated as immutable: `apply_observation` returns a new state.
    weights: dict[int, float] = field(default_factory=dict)
    last_observed_at: datetime | None = None
    #: Reels applied to this fingerprint (skipped Reels are not counted).
    n_reels: int = 0

    def raw_total(self) -> float:
        return math.fsum(self.weights[k] for k in sorted(self.weights))

    def share(self) -> dict[int, float]:
        """Display value `W_k / sum(W)`. Empty when nothing has been applied: no made-up shape."""
        total = self.raw_total()
        if total <= 0.0:
            return {}
        return {k: self.weights[k] / total for k in sorted(self.weights)}


@dataclass(frozen=True)
class Observation:
    event_id: UUID
    session_id: UUID
    observed_at: datetime
    dwell_ms: int | None
    video_duration_ms: int | None
    #: topic_id -> probability, summing to 1, or None if the Reel has no assignment.
    probabilities: Mapping[int, float] | None


@dataclass(frozen=True)
class Applied:
    state: FingerprintState
    #: Reason code when the Reel changed nothing (`no_assignment`, `no_dwell`), else None.
    skipped: str | None = None


def check_probabilities(probabilities: Mapping[int, float]) -> None:
    if not probabilities:
        raise FingerprintError("empty topic distribution")
    for p in probabilities.values():
        if not (math.isfinite(p) and p >= 0.0):
            raise FingerprintError("probability is not a finite non-negative number")
    if abs(math.fsum(probabilities.values()) - 1.0) > _PROB_SUM_TOLERANCE:
        raise FingerprintError("probabilities do not sum to 1")


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise FingerprintError("observed_at must be timezone-aware")


def apply_observation(
    state: FingerprintState, obs: Observation, config: FingerprintConfig
) -> Applied:
    _require_aware(obs.observed_at)
    if obs.probabilities is None:
        return Applied(state, "no_assignment")
    check_probabilities(obs.probabilities)
    if state.kind == "exposure":
        gain: float | None = exposure_gain()
    else:
        gain = engagement_gain(obs.dwell_ms, obs.video_duration_ms, config)
    if gain is None:
        return Applied(state, "no_dwell")

    factor = 1.0
    if state.last_observed_at is not None:
        _require_aware(state.last_observed_at)
        dt = (obs.observed_at - state.last_observed_at).total_seconds()
        if dt < 0.0:
            raise OutOfOrderError("observation is older than the state")
        factor = decay_factor(dt, config)

    weights: dict[int, float] = {}
    for k in sorted(set(state.weights) | set(obs.probabilities)):
        weights[k] = state.weights.get(k, 0.0) * factor + gain * obs.probabilities.get(k, 0.0)
    return Applied(
        replace(
            state,
            weights=weights,
            last_observed_at=obs.observed_at,
            n_reels=state.n_reels + 1,
        )
    )


# ---- snapshots and replay


@dataclass(frozen=True)
class SnapshotRecord:
    kind: FingerprintKind
    bucket: Literal["session", "day"]
    #: Deterministic: the `observed_at` of the last Reel that closed the bucket. Never `now()`.
    taken_at: datetime
    session_id: UUID | None
    weights: dict[str, Any]
    n_reels: int
    pipeline_version: str


def weights_document(
    state: FingerprintState,
    bucket_reels: int | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """JSON stored in `fingerprint_snapshots.weights`: raw weights (to resume) and the share.

    `bucket_reels` is how many Reels were applied inside the snapshot's own session or day. The
    row's `n_reels` is the running total, which says nothing about one session on its own.
    """
    document: dict[str, Any] = {
        "schema": WEIGHTS_SCHEMA,
        "raw": {str(k): state.weights[k] for k in sorted(state.weights)},
        "share": {str(k): v for k, v in state.share().items()},
        "last_observed_at": state.last_observed_at.isoformat() if state.last_observed_at else None,
    }
    if bucket_reels is not None:
        document["bucket_reels"] = bucket_reels
    if provenance is not None:
        document["provenance"] = dict(provenance)
    return document


def state_from_document(
    kind: FingerprintKind, document: Mapping[str, Any], n_reels: int
) -> FingerprintState:
    if document.get("schema") != WEIGHTS_SCHEMA:
        raise FingerprintError("unknown weights document")
    last = document.get("last_observed_at")
    return FingerprintState(
        kind=kind,
        weights={int(k): float(v) for k, v in document["raw"].items()},
        last_observed_at=datetime.fromisoformat(last) if last else None,
        n_reels=n_reels,
    )


@dataclass(frozen=True)
class SkippedReel:
    event_id: UUID
    kind: FingerprintKind
    reason: str


@dataclass(frozen=True)
class RebuildResult:
    pipeline_version: str
    states: dict[FingerprintKind, FingerprintState]
    #: Session and day snapshots, both kinds, in the order they were taken (time order).
    snapshots: tuple[SnapshotRecord, ...]
    skipped: tuple[SkippedReel, ...]


def snapshot_day(moment: datetime) -> date:
    """The UTC calendar day a snapshot belongs to.

    UTC is a placeholder for the user's own day (ADR-014's day timezone is Proposed). Shares do not
    change while time passes with no Reel (decay scales every weight alike), so a day's snapshot is
    the state after its last Reel, and a day with no Reel has no snapshot.
    """
    return moment.astimezone(UTC).date()


def order_key(obs: Observation) -> tuple[datetime, str]:
    """Application order: `(observed_at, event_id)` (ADR-011)."""
    return (obs.observed_at, str(obs.event_id))


def rebuild(
    observations: Iterable[Observation],
    config: FingerprintConfig,
    assignment_version: str,
) -> RebuildResult:
    """Build both fingerprints from scratch. Input order does not matter.

    This is the only place that walks a user's history; incremental updates and the replay script
    must call it (ADR-011), so "replay reproduces the snapshots" holds by construction.
    """
    ordered = sorted(observations, key=order_key)
    if len({o.event_id for o in ordered}) != len(ordered):
        raise FingerprintError("duplicate event_id")
    version = fingerprint_version(config, assignment_version)
    # Recorded in every snapshot, so the evidence view can rebuild each Reel's contribution from
    # exactly the rows and numbers the fingerprint used, not from today's settings.
    provenance = {"assignment_version": assignment_version, "params": asdict(config)}
    states: dict[FingerprintKind, FingerprintState] = {k: FingerprintState(k) for k in KINDS}
    last_of_session: dict[UUID, UUID] = {}
    last_of_day: dict[date, UUID] = {}
    for obs in ordered:
        last_of_session[obs.session_id] = obs.event_id
        last_of_day[snapshot_day(obs.observed_at)] = obs.event_id

    snapshots: list[SnapshotRecord] = []
    skipped: list[SkippedReel] = []
    in_session: dict[tuple[FingerprintKind, UUID], int] = {}
    in_day: dict[tuple[FingerprintKind, date], int] = {}
    for obs in ordered:
        day = snapshot_day(obs.observed_at)
        for kind in KINDS:
            result = apply_observation(states[kind], obs, config)
            states[kind] = result.state
            if result.skipped is not None:
                skipped.append(SkippedReel(obs.event_id, kind, result.skipped))
            else:
                in_session[(kind, obs.session_id)] = in_session.get((kind, obs.session_id), 0) + 1
                in_day[(kind, day)] = in_day.get((kind, day), 0) + 1
        if last_of_session[obs.session_id] == obs.event_id:
            for kind in KINDS:
                snapshots.append(
                    SnapshotRecord(
                        kind=kind,
                        bucket="session",
                        taken_at=obs.observed_at,
                        session_id=obs.session_id,
                        weights=weights_document(
                            states[kind], in_session.get((kind, obs.session_id), 0), provenance
                        ),
                        n_reels=states[kind].n_reels,
                        pipeline_version=version,
                    )
                )
        if last_of_day[day] == obs.event_id:
            for kind in KINDS:
                snapshots.append(
                    SnapshotRecord(
                        kind=kind,
                        bucket="day",
                        taken_at=obs.observed_at,
                        session_id=None,
                        weights=weights_document(
                            states[kind], in_day.get((kind, day), 0), provenance
                        ),
                        n_reels=states[kind].n_reels,
                        pipeline_version=version,
                    )
                )
    return RebuildResult(version, states, tuple(snapshots), tuple(skipped))

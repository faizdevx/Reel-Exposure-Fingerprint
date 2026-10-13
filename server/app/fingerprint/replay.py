"""Deterministic replay of a user's fingerprints, checked against the stored snapshots.

Given a user, a fingerprint `pipeline_version` and the stored topic assignments, rebuild both
fingerprints and every session and day snapshot, and compare them with what is stored. It never
writes: a replay is a check, not a repair (`app.manage rebuild-fingerprints` repairs).

Where the inputs come from. Nothing is taken from today's defaults:

- the assignment version and every parameter of the fingerprint (half-life, gains, skip rule) are
  read from the `provenance` stored in the snapshots of that version, and must hash back to the
  `pipeline_version` asked for (`fingerprint_version`); a snapshot that cannot say how it was made,
  or whose provenance disagrees with its version, is an error and not a guess;
- Reels and dwell come from the user's exposures, topic distributions from the stored assignments
  of that assignment version, in `(observed_at, event_id)` order (ADR-011).

What is compared, snapshot by snapshot (a snapshot is `(kind, session)` or `(kind, UTC day)`):
`taken_at`, `n_reels`, the number of Reels in the bucket, the last observation time, the
provenance, and every raw weight and displayed share. Floats match when
`|a - b| <= max(abs_tol, rel_tol * max(|a|, |b|))`; the defaults are `rel_tol = 1e-9` and
`abs_tol = 1e-12`. The same code on the same data gives bit-identical numbers (and the replay
checks that by running twice), so any difference above that tolerance is a real change in the
inputs or the code. A snapshot only on one side (a Reel deleted or added since, a stored row
missing) is a mismatch, never skipped.

`--from` and `--to` (UTC days, inclusive) choose which snapshots are compared. The replay always
starts from the user's first Reel, because every weight depends on the whole history before it.
"""

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import (
    FingerprintConfig,
    FingerprintError,
    SnapshotRecord,
    fingerprint_version,
    rebuild,
    snapshot_day,
)
from app.fingerprint.store import load_observations
from app.models import FingerprintSnapshot, Topic

REPLAY_FORMAT = "fingerprint-replay/1"
DEFAULT_REL_TOL = 1e-9
DEFAULT_ABS_TOL = 1e-12
MAX_DETAILS = 50


@dataclass(frozen=True)
class Tolerance:
    rel: float = DEFAULT_REL_TOL
    abs: float = DEFAULT_ABS_TOL

    def __post_init__(self) -> None:
        if (
            not (math.isfinite(self.rel) and math.isfinite(self.abs))
            or self.rel < 0
            or self.abs < 0
        ):
            raise ValueError("tolerances must be finite and non-negative")

    def close(self, a: float, b: float) -> bool:
        if not (math.isfinite(a) and math.isfinite(b)):
            return False  # NaN and infinity are never "the same number"
        return abs(a - b) <= max(self.abs, self.rel * max(abs(a), abs(b)))


@dataclass(frozen=True)
class StoredSnapshot:
    kind: str
    bucket: str
    taken_at: datetime
    session_id: uuid.UUID | None
    weights: Mapping[str, Any]
    n_reels: int


Key = tuple[str, str, str]


def key_of(kind: str, bucket: str, session_id: uuid.UUID | None, taken_at: datetime) -> Key:
    """`(kind, "session", session id)` or `(kind, "day", UTC date)`."""
    if bucket == "session":
        return (kind, bucket, str(session_id))
    return (kind, bucket, snapshot_day(taken_at).isoformat())


@dataclass
class Difference:
    key: Key
    field: str
    stored: Any
    replayed: Any
    abs_difference: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.key[0],
            "bucket": self.key[1],
            "snapshot": self.key[2],
            "field": self.field,
            "stored": self.stored,
            "replayed": self.replayed,
            "abs_difference": self.abs_difference,
        }


@dataclass
class ReplayReport:
    user_id: uuid.UUID
    pipeline_version: str
    status: str  # match | mismatch | no_snapshots | error
    reason: str | None = None
    assignment_version: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    tolerance: Tolerance = field(default_factory=Tolerance)
    deterministic: bool | None = None
    stored_snapshots: int = 0
    replayed_snapshots: int = 0
    compared: int = 0
    matched: int = 0
    only_in_stored: int = 0
    only_in_replay: int = 0
    values_compared: int = 0
    max_abs_difference: float = 0.0
    max_rel_difference: float = 0.0
    differences: list[Difference] = field(default_factory=list)
    differences_total: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "match"

    def as_dict(self) -> dict[str, Any]:
        """Strict JSON: ids, counts and numbers only, never Reel content."""
        return {
            "format": REPLAY_FORMAT,
            "user": str(self.user_id),
            "pipeline_version": self.pipeline_version,
            "assignment_version": self.assignment_version,
            "status": self.status,
            "reason": self.reason,
            "from": self.date_from.isoformat() if self.date_from else None,
            "to": self.date_to.isoformat() if self.date_to else None,
            "tolerance": {"rel": self.tolerance.rel, "abs": self.tolerance.abs},
            "deterministic": self.deterministic,
            "stored_snapshots": self.stored_snapshots,
            "replayed_snapshots": self.replayed_snapshots,
            "compared": self.compared,
            "matched": self.matched,
            "only_in_stored": self.only_in_stored,
            "only_in_replay": self.only_in_replay,
            "values_compared": self.values_compared,
            "max_abs_difference": self.max_abs_difference,
            "max_rel_difference": self.max_rel_difference,
            "differences_total": self.differences_total,
            "differences": [d.as_dict() for d in self.differences],
        }


def _error(report: ReplayReport, reason: str) -> ReplayReport:
    report.status, report.reason = "error", reason
    return report


# ------------------------------------------------------------------ comparison (pure)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _in_range(taken_at: datetime, start: date | None, end: date | None) -> bool:
    day = snapshot_day(taken_at)
    return (start is None or day >= start) and (end is None or day <= end)


class _Comparer:
    def __init__(self, report: ReplayReport) -> None:
        self.report = report
        self.tol = report.tolerance

    def differ(
        self, key: Key, name: str, stored: Any, replayed: Any, gap: float | None = None
    ) -> None:
        self.report.differences_total += 1
        if len(self.report.differences) < MAX_DETAILS:
            self.report.differences.append(Difference(key, name, stored, replayed, gap))

    def exact(self, key: Key, name: str, stored: Any, replayed: Any) -> bool:
        if stored == replayed:
            return True
        self.differ(key, name, stored, replayed)
        return False

    def number(self, key: Key, name: str, stored: Any, replayed: Any) -> bool:
        self.report.values_compared += 1
        if not (isinstance(stored, int | float) and isinstance(replayed, int | float)):
            self.differ(key, name, stored, replayed)
            return False
        a, b = float(stored), float(replayed)
        if not (math.isfinite(a) and math.isfinite(b)):
            self.differ(
                key,
                name,
                stored if math.isfinite(a) else None,
                replayed if math.isfinite(b) else None,
            )
            return False
        gap = abs(a - b)
        scale = max(abs(a), abs(b))
        self.report.max_abs_difference = max(self.report.max_abs_difference, gap)
        if scale > 0.0:
            self.report.max_rel_difference = max(self.report.max_rel_difference, gap / scale)
        if self.tol.close(a, b):
            return True
        self.differ(key, name, a, b, gap)
        return False

    def mapping(self, key: Key, name: str, stored: Any, replayed: Any) -> bool:
        if not isinstance(stored, Mapping) or not isinstance(replayed, Mapping):
            self.differ(
                key,
                name,
                "not a mapping" if not isinstance(stored, Mapping) else "ok",
                "ok" if isinstance(stored, Mapping) else "not a mapping",
            )
            return False
        ok = True
        for topic in sorted(set(stored) | set(replayed), key=lambda t: int(t)):
            if topic not in stored or topic not in replayed:
                self.differ(
                    key,
                    f"{name}[{topic}]",
                    stored.get(topic, "absent"),
                    replayed.get(topic, "absent"),
                )
                ok = False
            elif not self.number(key, f"{name}[{topic}]", stored[topic], replayed[topic]):
                ok = False
        return ok

    def snapshot(self, key: Key, stored: StoredSnapshot, replayed: SnapshotRecord) -> bool:
        ok = True
        ok &= self.exact(key, "taken_at", stored.taken_at, replayed.taken_at)
        ok &= self.exact(key, "n_reels", stored.n_reels, replayed.n_reels)
        s, r = stored.weights, replayed.weights
        ok &= self.exact(key, "schema", s.get("schema"), r.get("schema"))
        ok &= self.exact(key, "bucket_reels", s.get("bucket_reels"), r.get("bucket_reels"))
        ok &= self.exact(
            key,
            "last_observed_at",
            _parse_time(s.get("last_observed_at")),
            _parse_time(r.get("last_observed_at")),
        )
        ok &= self.exact(
            key,
            "provenance",
            json.loads(json.dumps(s.get("provenance"))),
            json.loads(json.dumps(r.get("provenance"))),
        )
        ok &= self.mapping(key, "raw", s.get("raw"), r.get("raw"))
        ok &= self.mapping(key, "share", s.get("share"), r.get("share"))
        return ok


def compare_snapshots(
    stored: Sequence[StoredSnapshot],
    replayed: Sequence[SnapshotRecord],
    report: ReplayReport,
) -> None:
    """Fill `report` from the two sets. A snapshot is in range when either side's day is."""
    comparer = _Comparer(report)
    stored_by: dict[Key, StoredSnapshot] = {}
    for snap in stored:
        key = key_of(snap.kind, snap.bucket, snap.session_id, snap.taken_at)
        if key in stored_by:  # two stored rows for one snapshot: the store would never write this
            comparer.differ(key, "duplicate_stored_row", 2, 1)
        stored_by[key] = snap
    replayed_by = {key_of(r.kind, r.bucket, r.session_id, r.taken_at): r for r in replayed}
    report.stored_snapshots = len(stored_by)
    report.replayed_snapshots = len(replayed_by)

    for key in sorted(set(stored_by) | set(replayed_by)):
        left, right = stored_by.get(key), replayed_by.get(key)
        times = [t.taken_at for t in (left, right) if t is not None]
        if not any(_in_range(t, report.date_from, report.date_to) for t in times):
            continue
        if left is None:
            report.only_in_replay += 1
            comparer.differ(key, "snapshot", "absent", "present")
            continue
        if right is None:
            report.only_in_stored += 1
            comparer.differ(key, "snapshot", "present", "absent")
            continue
        report.compared += 1
        before = report.differences_total
        comparer.snapshot(key, left, right)
        if report.differences_total == before:
            report.matched += 1


# ------------------------------------------------------------------ the replay (database)


def replay_user(
    db: DbSession,
    user_id: uuid.UUID,
    pipeline_version: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    tolerance: Tolerance | None = None,
) -> ReplayReport:
    """Rebuild one user's snapshots of one fingerprint version and compare them. Read-only."""
    report = ReplayReport(
        user_id=user_id,
        pipeline_version=pipeline_version,
        status="match",
        date_from=date_from,
        date_to=date_to,
        tolerance=tolerance or Tolerance(),
    )
    if date_from is not None and date_to is not None and date_from > date_to:
        return _error(report, "from_after_to")

    rows = list(
        db.execute(
            select(FingerprintSnapshot).where(
                FingerprintSnapshot.user_id == user_id,
                FingerprintSnapshot.pipeline_version == pipeline_version,
            )
        ).scalars()
    )
    if not rows:
        report.status = "no_snapshots"
        report.reason = "no stored snapshots of this user and version"
        return report
    stored = [
        StoredSnapshot(r.kind, r.bucket, r.taken_at, r.session_id, r.weights, int(r.n_reels))
        for r in rows
    ]

    provenances = {json.dumps(s.weights.get("provenance"), sort_keys=True) for s in stored}
    if provenances == {"null"}:
        return _error(report, "no_provenance")
    if len(provenances) != 1:
        return _error(report, "inconsistent_provenance")
    provenance = stored[0].weights["provenance"]
    try:
        assignment_version = str(provenance["assignment_version"])
        config = FingerprintConfig(**provenance["params"])
    except (KeyError, TypeError, ValueError):
        return _error(report, "bad_provenance")
    report.assignment_version = assignment_version
    if fingerprint_version(config, assignment_version) != pipeline_version:
        return _error(report, "provenance_does_not_match_version")

    topic_ids = [t for (t,) in db.execute(select(Topic.id).where(Topic.id != 0))]
    observations = load_observations(db, user_id, assignment_version, topic_ids)
    try:
        first = rebuild(observations, config, assignment_version)
        second = rebuild(list(reversed(observations)), config, assignment_version)
    except FingerprintError:
        # The stored inputs are no longer valid (a distribution that does not sum to 1, a duplicate
        # event): the engine refuses them, and so does the replay, with a reason and no snapshots.
        return _error(report, "inputs_rejected_by_engine")
    report.deterministic = first.snapshots == second.snapshots

    compare_snapshots(stored, first.snapshots, report)
    if report.differences_total or not report.deterministic:
        report.status = "mismatch"
    return report


def users_with_snapshots(db: DbSession, pipeline_version: str) -> list[uuid.UUID]:
    rows = db.execute(
        select(FingerprintSnapshot.user_id)
        .where(FingerprintSnapshot.pipeline_version == pipeline_version)
        .distinct()
        .order_by(FingerprintSnapshot.user_id)
    ).all()
    return [row[0] for row in rows]

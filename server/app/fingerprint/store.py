"""Database edge of the fingerprint engine: read observations, write snapshot rows.

`load_observations` joins a user's exposures with the stored topic assignments of one assignment
version. A Reel without a complete assignment set gets `probabilities=None` and is skipped by the
engine, not guessed. `store_snapshots` replaces the snapshots of the sessions it is given for the
same pipeline version, so running a rebuild twice leaves one set of rows, not two.

Never touches `topic_transitions` or `predictions`. No per-user state table (ADR-011 is Proposed).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import (
    FingerprintConfig,
    Observation,
    RebuildResult,
    SnapshotRecord,
    rebuild,
)
from app.models import FingerprintSnapshot, ReelExposure, TopicAssignment
from app.worker.fusion import OTHER_TOPIC_ID


def load_observations(
    db: DbSession,
    user_id: uuid.UUID,
    assignment_version: str,
    expected_topic_ids: Iterable[int],
) -> list[Observation]:
    wanted = frozenset(expected_topic_ids)
    exposures = list(
        db.execute(select(ReelExposure).where(ReelExposure.user_id == user_id)).scalars()
    )
    shortcodes = {e.reel_shortcode for e in exposures}
    by_reel: dict[str, dict[int, float]] = defaultdict(dict)
    if shortcodes:
        # All of the user's assignments of this version, streamed, and kept when the user has an
        # exposure of that Reel. Not `reel_shortcode IN (...)`: a user with more than 65,535
        # distinct Reels (N11 allows 100,000) overflows the 65,535 bound parameters one query may
        # carry and `rebuild_user` fails (found by the performance benchmark).
        rows = db.execute(
            select(
                TopicAssignment.reel_shortcode, TopicAssignment.topic_id, TopicAssignment.p_fused
            )
            .where(
                TopicAssignment.user_id == user_id,
                TopicAssignment.pipeline_version == assignment_version,
            )
            .execution_options(yield_per=50_000)
        )
        for shortcode, topic_id, p_fused in rows:
            if shortcode in shortcodes:
                by_reel[shortcode][int(topic_id)] = float(p_fused)
    return [
        Observation(
            event_id=e.event_id,
            session_id=e.session_id,
            observed_at=e.observed_at,
            dwell_ms=e.dwell_ms,
            video_duration_ms=e.video_duration_ms,
            probabilities=by_reel[e.reel_shortcode]
            if set(by_reel.get(e.reel_shortcode, {})) in (wanted, wanted | {OTHER_TOPIC_ID})
            else None,
        )
        for e in exposures
    ]


def store_snapshots(db: DbSession, user_id: uuid.UUID, records: Iterable[SnapshotRecord]) -> int:
    """Insert snapshots, replacing earlier ones, so a rebuild leaves one set of rows.

    Session snapshots replace those of the same user, session and version. Day snapshots replace
    every day snapshot of the same user and version: a rebuild walks the whole history, so the days
    it produces are the days there are (a day that no longer has a Reel must not linger).
    """
    items = list(records)
    for version in {r.pipeline_version for r in items if r.bucket == "day"}:
        db.execute(
            delete(FingerprintSnapshot).where(
                FingerprintSnapshot.user_id == user_id,
                FingerprintSnapshot.bucket == "day",
                FingerprintSnapshot.pipeline_version == version,
            )
        )
    for session_id, version in {
        (r.session_id, r.pipeline_version) for r in items if r.bucket == "session"
    }:
        db.execute(
            delete(FingerprintSnapshot).where(
                FingerprintSnapshot.user_id == user_id,
                FingerprintSnapshot.bucket == "session",
                FingerprintSnapshot.session_id == session_id,
                FingerprintSnapshot.pipeline_version == version,
            )
        )
    for r in items:
        db.add(
            FingerprintSnapshot(
                user_id=user_id,
                kind=r.kind,
                bucket=r.bucket,
                taken_at=r.taken_at,
                session_id=r.session_id,
                weights=r.weights,
                n_reels=r.n_reels,
                pipeline_version=r.pipeline_version,
            )
        )
    db.flush()
    return len(items)


def rebuild_user(
    db: DbSession,
    user_id: uuid.UUID,
    *,
    assignment_version: str,
    topic_ids: Iterable[int],
    config: FingerprintConfig,
) -> RebuildResult:
    """Rebuild one user's fingerprints from stored assignments and store session and day snapshots.

    The replay of M5: the same stored assignments and the same configuration always give the same
    snapshots (`rebuild` is deterministic and takes no clock). The caller commits.
    """
    observations = load_observations(db, user_id, assignment_version, topic_ids)
    result = rebuild(observations, config, assignment_version)
    store_snapshots(db, user_id, result.snapshots)
    return result


def users_with_exposures(db: DbSession) -> list[uuid.UUID]:
    rows = db.execute(select(ReelExposure.user_id).distinct().order_by(ReelExposure.user_id)).all()
    return [row[0] for row in rows]

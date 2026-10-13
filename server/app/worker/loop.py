"""Job loop: claims `exposure` jobs, runs the Reel pipeline, rebuilds fingerprints.

One process. A job is claimed with `FOR UPDATE SKIP LOCKED` (spec §5), so a second
worker would not take the same row. The queue is the `jobs` table; there is no Redis.

Per job:

    claim -> load exposure -> process_reel (text; frames are not captured yet, so none) ->
    exposure.process_status = done -> job done

A failure re-queues the job with a growing delay, up to `MAX_ATTEMPTS`, then marks it `failed`.
`jobs.last_error` holds the exception class name only: messages can quote captions.

When the queue is empty and users got new assignments since the last rebuild, their fingerprints,
snapshots and predictions are rebuilt once from stored assignments (replay, M5), not per Reel.

Logs carry ids, counts, durations and error classes only.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text, update
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import FingerprintConfig
from app.fingerprint.store import rebuild_user
from app.models import Job, ReelExposure
from app.prediction.config import PredictionConfig
from app.prediction.store import rebuild_predictions
from app.worker.reel_processor import ReelPipeline, process_reel
from app.worker.text_normalize import TextInput

log = logging.getLogger("app.worker.loop")

EXPOSURE_JOB = "exposure"
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 30

_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE kind = :kind AND status = 'queued' AND run_after <= now()
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, payload, attempts
    """
)


@dataclass(frozen=True)
class Claimed:
    job_id: int
    event_id: uuid.UUID
    attempts: int


def claim_job(db: DbSession) -> Claimed | None:
    row = db.execute(_CLAIM, {"kind": EXPOSURE_JOB}).first()
    db.commit()
    if row is None:
        return None
    job_id, payload, attempts = row
    return Claimed(int(job_id), uuid.UUID(str(payload["event_id"])), int(attempts))


def _finish(db: DbSession, job_id: int, status: str, error: str | None = None) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(status=status, last_error=error))
    db.commit()


def _retry_or_fail(db: DbSession, claimed: Claimed, error: str) -> str:
    if claimed.attempts >= MAX_ATTEMPTS:
        _finish(db, claimed.job_id, "failed", error)
        return "failed"
    delay = timedelta(seconds=RETRY_BASE_SECONDS * 2 ** (claimed.attempts - 1))
    db.execute(
        update(Job)
        .where(Job.id == claimed.job_id)
        .values(status="queued", last_error=error, run_after=datetime.now(UTC) + delay)
    )
    db.commit()
    return "retry"


def run_one(db: DbSession, pipeline: ReelPipeline) -> tuple[str, uuid.UUID | None] | None:
    """Claim and process one job. Returns (outcome, user_id), or None when the queue is empty."""
    claimed = claim_job(db)
    if claimed is None:
        return None
    t0 = time.monotonic()
    try:
        exposure = db.get(ReelExposure, claimed.event_id)
        if exposure is None:  # deleted by the user while queued
            _finish(db, claimed.job_id, "done", "exposure_gone")
            return "gone", None
        user_id = exposure.user_id
        hashtags = tuple(exposure.hashtags) if exposure.hashtags is not None else None
        outcome = process_reel(
            db,
            pipeline,
            user_id,
            exposure.reel_shortcode,
            TextInput(caption=exposure.caption, hashtags=hashtags),
        )
        failed = [r.stage for r in outcome.reports if r.status == "failed"]
        if failed and not outcome.p_fused:
            raise RuntimeError(f"stages failed: {','.join(failed)}")
        exposure.process_status = "done" if outcome.p_fused else "no_signal"
        db.commit()
        _finish(db, claimed.job_id, "done")
        log.info(
            "job=%s event=%s status=%s cached=%s modalities=%s duration_ms=%.0f",
            claimed.job_id,
            claimed.event_id,
            exposure.process_status,
            outcome.from_cache,
            ",".join(outcome.modalities),
            (time.monotonic() - t0) * 1000,
        )
        return "done", user_id
    except Exception as exc:
        db.rollback()
        error = type(exc).__name__
        result = _retry_or_fail(db, claimed, error)
        log.warning(
            "job=%s event=%s status=%s attempt=%d error=%s",
            claimed.job_id,
            claimed.event_id,
            result,
            claimed.attempts,
            error,
        )
        return result, None


def rebuild_users(db: DbSession, pipeline: ReelPipeline, users: set[uuid.UUID]) -> None:
    fp_config = FingerprintConfig()
    topic_ids = [t.id for t in pipeline.taxonomy.topics]
    for user_id in sorted(users, key=str):
        try:
            result = rebuild_user(
                db,
                user_id,
                assignment_version=pipeline.version,
                topic_ids=topic_ids,
                config=fp_config,
            )
            rebuilt = rebuild_predictions(
                db,
                user_id,
                assignment_version=pipeline.version,
                fp_config=fp_config,
                config=PredictionConfig(),
            )
            db.commit()
            log.info(
                "rebuilt user=%s snapshots=%d skipped_reels=%d prediction_rows=%d",
                user_id,
                len(result.snapshots),
                len(result.skipped),
                rebuilt.prediction_rows,
            )
        except Exception as exc:
            db.rollback()
            log.warning("rebuild user=%s error=%s", user_id, type(exc).__name__)


def run_forever(
    db: DbSession, pipeline: ReelPipeline, *, poll_seconds: float = 2.0, once: bool = False
) -> None:
    """Process jobs until stopped. `once` drains the queue, rebuilds, then returns."""
    dirty: set[uuid.UUID] = set()
    while True:
        result = run_one(db, pipeline)
        if result is not None:
            outcome, user_id = result
            if outcome == "done" and user_id is not None:
                dirty.add(user_id)
            continue
        if dirty:
            rebuild_users(db, pipeline, dirty)
            dirty = set()
        if once:
            return
        time.sleep(poll_seconds)

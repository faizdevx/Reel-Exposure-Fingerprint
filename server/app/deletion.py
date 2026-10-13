"""Complete deletion of a session, or of everything a user has (spec F13, N7, ADR-012).

Two operations, both idempotent, both finished only when a verification pass says so:

- `delete_session`: the session and everything derived from it.
- `delete_all_data`: everything of one user, the account included.

What "everything" means (each is removed explicitly, none is left to a cascade alone):

    sessions, reel_exposures                       the observations
    jobs                                           jobs of those exposures (no FK: matched by
                                                   payload event_id)
    predictions, topic_transitions                 derived, cumulative over the user's history
    fingerprint_snapshots                          derived, cumulative: later snapshots contain what
                                                   earlier Reels added
    content_features, topic_assignments            the user's own Reel cache (ADR-024): removed for
                                                   every Reel of that user no remaining exposure of
                                                   theirs refers to; no other user's rows are read
    frames on disk                                 raw JPEGs under the frame store
    (delete all only) consents, extension_tokens, dashboard logins, invites used, and the user row

Session deletion removes derived data that included the session and then rebuilds the user's
fingerprints from the Reels that remain (from the settings each snapshot recorded), so no snapshot
keeps weight from a deleted Reel. Predictions and topic transitions have no rebuild yet (M6); they
are deleted whole and the future rebuild regenerates them.

Order and safety:

1. Database rows are deleted and committed. For delete-all the account row, the dashboard login
   and the tokens that identify the caller stay until the very end, so a failure midway can be
   retried by the same caller.
2. Files are deleted after the commit.
3. A fresh verification pass counts what is left in every table and on disk. Only when every
   count is zero is the deletion reported done; otherwise `DeletionIncomplete` carries the counts,
   the endpoint answers 500, and calling again finishes the job.
4. For delete-all, the account is deleted last, then verified again.

A repeat call finds nothing, deletes nothing, and verifies zero: success again. Nothing here logs
captions, handles or text: only ids and counts.
"""

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import CursorResult, and_, cast, delete, exists, func, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import FingerprintConfig
from app.fingerprint.store import rebuild_user
from app.frames import FrameStore
from app.models import (
    Consent,
    ContentFeature,
    ExtensionToken,
    FingerprintSnapshot,
    Invite,
    Job,
    Prediction,
    PredictionState,
    ReelExposure,
    SelfReportRating,
    SurveyResponse,
    Topic,
    TopicAssignment,
    TopicTransition,
    User,
    UserSession,
)
from app.models import Session as MonitoringSession
from app.prediction.config import PredictionConfig
from app.prediction.store import rebuild_predictions
from app.worker.fusion import OTHER_TOPIC_ID

log = logging.getLogger("app.deletion")

_CHUNK = 500
EXPOSURE_JOB = "exposure"  # the job kind `routers.events` enqueues


@dataclass
class DeletionReport:
    scope: str  # "session" | "all"
    existed: bool
    #: Rows deleted, per table.
    deleted: dict[str, int] = field(default_factory=dict)
    #: What the verification pass still found, per table and `frame_files`. All zero when done.
    remaining: dict[str, int] = field(default_factory=dict)
    frame_files_deleted: int = 0
    #: Fingerprints rebuilt from the remaining Reels, and snapshots dropped without a rebuild
    #: because they predate the record of how they were built.
    fingerprints_rebuilt: int = 0
    predictions_rebuilt: int = 0
    fingerprints_dropped: int = 0

    @property
    def verified(self) -> bool:
        return bool(self.remaining) and all(v == 0 for v in self.remaining.values())

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["verified"] = self.verified
        return out


class DeletionIncomplete(Exception):
    """The verification pass found something left. The report says what; a retry finishes."""

    def __init__(self, report: DeletionReport) -> None:
        super().__init__("deletion_incomplete")
        self.report = report


# ------------------------------------------------------------------ small helpers


def _chunks(items: Sequence[str], size: int = _CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _count(db: DbSession, statement: Any) -> int:
    return int(db.execute(statement).scalar_one())


def _del(db: DbSession, statement: Any) -> int:
    """Run a DELETE and return how many rows it removed."""
    result: CursorResult[Any] = db.execute(statement)  # type: ignore[assignment]
    return int(result.rowcount or 0)


def _jobs_for_events(db: DbSession, event_ids: Sequence[str]) -> int:
    deleted = 0
    for chunk in _chunks(event_ids):
        deleted += _del(db, delete(Job).where(Job.payload["event_id"].astext.in_(list(chunk))))
    return deleted


def _count_jobs_for_events(db: DbSession, event_ids: Sequence[str]) -> int:
    """Jobs that name one of these events and are not already counted as orphans."""
    total = 0
    for chunk in _chunks(event_ids):
        total += _count(
            db,
            select(func.count())
            .select_from(Job)
            .where(Job.payload["event_id"].astext.in_(list(chunk)), ~_orphan_jobs()),
        )
    return total


def _unreferenced(model: Any) -> Any:
    """True for a cache row whose Reel no exposure of the row's own user refers to any more."""
    return ~exists().where(
        ReelExposure.user_id == model.user_id,
        ReelExposure.reel_shortcode == model.reel_shortcode,
    )


def _sweep_cache(db: DbSession) -> dict[str, int]:
    """Drop every cache row of a user's Reel that none of that user's exposures refers to any more.

    Not limited to the Reels of this deletion: a cache row nobody refers to is garbage whoever
    left it, and sweeping all of it means a deletion that failed halfway is finished by a retry
    (the exposures that named those Reels are gone by then).
    """
    return {
        "content_features": _del(db, delete(ContentFeature).where(_unreferenced(ContentFeature))),
        "topic_assignments": _del(
            db, delete(TopicAssignment).where(_unreferenced(TopicAssignment))
        ),
    }


def _orphan_jobs() -> Any:
    """Exposure jobs whose exposure no longer exists."""
    return and_(
        Job.kind == EXPOSURE_JOB,
        ~exists().where(
            ReelExposure.event_id == cast(Job.payload["event_id"].astext, PG_UUID(as_uuid=True))
        ),
    )


def _sweep_orphan_jobs(db: DbSession) -> int:
    return _del(db, delete(Job).where(_orphan_jobs()))


def _count_orphan_cache(db: DbSession) -> dict[str, int]:
    return {
        "content_features": _count(
            db,
            select(func.count()).select_from(ContentFeature).where(_unreferenced(ContentFeature)),
        ),
        "topic_assignments": _count(
            db,
            select(func.count()).select_from(TopicAssignment).where(_unreferenced(TopicAssignment)),
        ),
    }


def _count_orphan_jobs(db: DbSession) -> int:
    return _count(db, select(func.count()).select_from(Job).where(_orphan_jobs()))


def _event_ids_of(db: DbSession, user_id: uuid.UUID) -> set[str]:
    return {
        str(e)
        for (e,) in db.execute(select(ReelExposure.event_id).where(ReelExposure.user_id == user_id))
    }


def _add(report: DeletionReport, table: str, count: int) -> None:
    report.deleted[table] = report.deleted.get(table, 0) + count


@dataclass
class Recipes:
    """How the user's derived data was built, read before it is deleted, to build it again."""

    fingerprints: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)


def _delete_derived(db: DbSession, user_id: uuid.UUID, report: DeletionReport) -> Recipes:
    """Delete the user's predictions, transitions and snapshots; return how snapshots were built.

    These are cumulative over the user's whole history, so any deletion of a Reel makes every
    one of them stale. Returns the distinct `(assignment_version, params)` recorded in the
    snapshots, to rebuild from; snapshots without that record are only counted as dropped.
    """
    recipes: dict[str, dict[str, Any]] = {}
    dropped = 0
    for (document,) in db.execute(
        select(FingerprintSnapshot.weights).where(FingerprintSnapshot.user_id == user_id)
    ):
        provenance = document.get("provenance") if isinstance(document, dict) else None
        if (
            isinstance(provenance, dict)
            and "assignment_version" in provenance
            and "params" in provenance
        ):
            recipes[json.dumps(provenance, sort_keys=True)] = provenance
        else:
            dropped += 1
    report.fingerprints_dropped = dropped
    prediction_recipes: dict[str, dict[str, Any]] = {}
    for (state,) in db.execute(
        select(PredictionState.state).where(PredictionState.user_id == user_id)
    ):
        if all(
            k in state for k in ("assignment_version", "fingerprint_params", "prediction_params")
        ):
            recipe = {
                k: state[k]
                for k in ("assignment_version", "fingerprint_params", "prediction_params")
            }
            prediction_recipes[json.dumps(recipe, sort_keys=True)] = recipe
    _add(
        report,
        "prediction_state",
        _del(db, delete(PredictionState).where(PredictionState.user_id == user_id)),
    )
    _add(report, "predictions", _del(db, delete(Prediction).where(Prediction.user_id == user_id)))
    _add(
        report,
        "topic_transitions",
        _del(db, delete(TopicTransition).where(TopicTransition.user_id == user_id)),
    )
    _add(
        report,
        "fingerprint_snapshots",
        _del(db, delete(FingerprintSnapshot).where(FingerprintSnapshot.user_id == user_id)),
    )
    return Recipes(list(recipes.values()), list(prediction_recipes.values()))


def _rebuild(db: DbSession, user_id: uuid.UUID, recipes: Recipes, report: DeletionReport) -> None:
    topic_ids = [t for (t,) in db.execute(select(Topic.id).where(Topic.id != OTHER_TOPIC_ID))]
    for recipe in recipes.fingerprints:
        try:
            config = FingerprintConfig(**recipe["params"])
        except (TypeError, ValueError):
            report.fingerprints_dropped += 1
            continue
        rebuild_user(
            db,
            user_id,
            assignment_version=str(recipe["assignment_version"]),
            topic_ids=topic_ids,
            config=config,
        )
        report.fingerprints_rebuilt += 1
    for recipe in recipes.predictions:
        try:
            fp_config = FingerprintConfig(**recipe["fingerprint_params"])
            params = dict(recipe["prediction_params"])
            params["lambda_grid"] = tuple(params["lambda_grid"])
            pred_config = PredictionConfig(**params)
        except (TypeError, ValueError, KeyError):
            continue
        rebuild_predictions(
            db,
            user_id,
            assignment_version=str(recipe["assignment_version"]),
            fp_config=fp_config,
            config=pred_config,
        )
        report.predictions_rebuilt += 1


# ------------------------------------------------------------------ files, tolerant of failure


def _prune_files(store: FrameStore, user_id: uuid.UUID, keep: set[str]) -> int:
    """Delete the user's frames that no existing event owns. A failure is logged and counted by
    the verification that follows (files left), never swallowed into a success."""
    try:
        return store.prune_user(user_id, keep)
    except OSError as error:
        log.error("frame deletion failed user=%s error=%s", user_id, type(error).__name__)
        return 0


# ------------------------------------------------------------------ one session


def _verify_session(
    db: DbSession,
    store: FrameStore,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    event_ids: Sequence[str],
) -> dict[str, int]:
    db.rollback()  # read what is committed, not what this transaction remembers
    cache = _count_orphan_cache(db)
    event_uuids = [uuid.UUID(e) for e in event_ids]
    predictions = 0
    for start in range(0, len(event_uuids), _CHUNK):
        predictions += _count(
            db,
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.made_before_event.in_(event_uuids[start : start + _CHUNK])),
        )
    return {
        "sessions": _count(
            db,
            select(func.count())
            .select_from(MonitoringSession)
            .where(MonitoringSession.id == session_id, MonitoringSession.user_id == user_id),
        ),
        "reel_exposures": _count(
            db,
            select(func.count())
            .select_from(ReelExposure)
            .where(ReelExposure.session_id == session_id, ReelExposure.user_id == user_id),
        ),
        "jobs": _count_jobs_for_events(db, event_ids) + _count_orphan_jobs(db),
        "predictions": predictions,
        "fingerprint_snapshots": _count(
            db,
            select(func.count())
            .select_from(FingerprintSnapshot)
            .where(
                FingerprintSnapshot.session_id == session_id,
                FingerprintSnapshot.user_id == user_id,
            ),
        ),
        "content_features": cache["content_features"],
        "topic_assignments": cache["topic_assignments"],
        # Anything in the user's frame folder that no existing event owns.
        "frame_files": store.user_files_outside(user_id, _event_ids_of(db, user_id)),
    }


def delete_session(
    db: DbSession, store: FrameStore, user_id: uuid.UUID, session_id: uuid.UUID
) -> DeletionReport:
    """Delete one of the user's sessions and everything derived from it. Idempotent.

    A session that does not exist for this user (never existed, already deleted, or someone
    else's) is not an error: the report says `existed: false` and the session table is not
    touched. The call still clears what a failed earlier attempt left behind for this user (frame
    folders and jobs of exposures that no longer exist, cache rows nobody refers to), which is how
    a retry finishes the job. An active session is deleted too; its extension's later events are
    refused as `session_not_found`.
    """
    report = DeletionReport(scope="session", existed=False)
    session = db.execute(
        select(MonitoringSession)
        .where(MonitoringSession.id == session_id, MonitoringSession.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()

    event_ids: list[str] = []
    recipes = Recipes()
    if session is not None:
        report.existed = True
        event_ids = [
            str(e)
            for (e,) in db.execute(
                select(ReelExposure.event_id).where(
                    ReelExposure.session_id == session_id, ReelExposure.user_id == user_id
                )
            )
        ]
        _add(report, "jobs", _jobs_for_events(db, event_ids))
        recipes = _delete_derived(db, user_id, report)
        _add(
            report,
            "reel_exposures",
            _del(
                db,
                delete(ReelExposure).where(
                    ReelExposure.session_id == session_id, ReelExposure.user_id == user_id
                ),
            ),
        )
        _add(
            report,
            "sessions",
            _del(
                db,
                delete(MonitoringSession).where(
                    MonitoringSession.id == session_id, MonitoringSession.user_id == user_id
                ),
            ),
        )
    _add(report, "jobs", _sweep_orphan_jobs(db))
    for table, count in _sweep_cache(db).items():
        _add(report, table, count)
    db.flush()
    if session is not None:
        _rebuild(db, user_id, recipes, report)
    db.commit()

    # Files last, after the rows: whatever no existing event owns goes (this session's frames,
    # and any left over by an earlier failed attempt).
    report.frame_files_deleted = _prune_files(store, user_id, _event_ids_of(db, user_id))

    report.remaining = _verify_session(db, store, user_id, session_id, event_ids)
    log.info(
        "delete session=%s existed=%s verified=%s", session_id, report.existed, report.verified
    )
    if not report.verified:
        raise DeletionIncomplete(report)
    return report


# ------------------------------------------------------------------ everything of a user


USER_TABLES: dict[str, Any] = {
    "extension_tokens": ExtensionToken.user_id,
    "consents": Consent.user_id,
    "sessions": MonitoringSession.user_id,
    "reel_exposures": ReelExposure.user_id,
    "fingerprint_snapshots": FingerprintSnapshot.user_id,
    "topic_transitions": TopicTransition.user_id,
    "predictions": Prediction.user_id,
    "prediction_state": PredictionState.user_id,
    "self_report_ratings": SelfReportRating.user_id,
    "survey_responses": SurveyResponse.user_id,
    "user_sessions": UserSession.user_id,
    "invites": Invite.used_by,
}


ACCOUNT_TABLES = frozenset({"user_sessions", "invites"})


def _verify_user(
    db: DbSession,
    store: FrameStore,
    user_id: uuid.UUID,
    event_ids: Sequence[str],
    *,
    account: bool = True,
) -> dict[str, int]:
    """What is left of a user. `account=False` leaves out the account, its login and invites:
    they are deleted last, and only once everything else here is zero."""
    db.rollback()
    remaining: dict[str, int] = {}
    for table, column in USER_TABLES.items():
        if not account and table in ACCOUNT_TABLES:
            continue
        remaining[table] = _count(
            db, select(func.count()).select_from(column.class_).where(column == user_id)
        )
    if account:
        remaining["users"] = _count(
            db, select(func.count()).select_from(User).where(User.id == user_id)
        )
    remaining["jobs"] = _count_jobs_for_events(db, event_ids) + _count_orphan_jobs(db)
    cache = _count_orphan_cache(db)
    remaining["content_features"] = cache["content_features"]
    remaining["topic_assignments"] = cache["topic_assignments"]
    remaining["frame_files"] = store.user_files_outside(user_id, set())
    return remaining


def delete_all_data(db: DbSession, store: FrameStore, user_id: uuid.UUID) -> DeletionReport:
    """Delete everything of one user, the account last. Idempotent and retryable.

    Phase 1 deletes every row except the account, the dashboard login and the invites that name
    the user; the extension is cut off at once (its tokens and consent go). Phase 2 deletes the
    frame folder. Phase 3 deletes the account. Each phase is safe to repeat, and a failure before
    phase 3 leaves the caller able to log in and call again.
    """
    report = DeletionReport(scope="all", existed=False)
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()

    event_ids: list[str] = []
    if user is not None:
        report.existed = True
        event_ids = sorted(_event_ids_of(db, user_id))
        _add(report, "jobs", _jobs_for_events(db, event_ids))
        _delete_derived(db, user_id, report)  # nothing is rebuilt: no Reels will remain
        report.fingerprints_dropped = 0
        for table in (
            "reel_exposures",
            "sessions",
            "extension_tokens",
            "consents",
            "self_report_ratings",
            "survey_responses",
        ):
            column = USER_TABLES[table]
            _add(report, table, _del(db, delete(column.class_).where(column == user_id)))
        _add(report, "jobs", _sweep_orphan_jobs(db))
        for table, count in _sweep_cache(db).items():
            _add(report, table, count)
        db.commit()

    # Phase 2: files. Runs even when the user row is already gone (a retry after a failure).
    try:
        report.frame_files_deleted = store.delete_user(user_id)
    except OSError as error:
        log.error("frame deletion failed user=%s error=%s", user_id, type(error).__name__)

    # Phase 3: the account, last. Only once everything else verifies clean: otherwise the account
    # (and so the login) is kept, and the caller can call again to finish.
    leftovers = _verify_user(db, store, user_id, event_ids, account=False)
    if user is not None and all(v == 0 for v in leftovers.values()):
        _add(
            report,
            "user_sessions",
            _del(db, delete(UserSession).where(UserSession.user_id == user_id)),
        )
        _add(report, "invites", _del(db, delete(Invite).where(Invite.used_by == user_id)))
        _add(report, "users", _del(db, delete(User).where(User.id == user_id)))
        db.commit()

    report.remaining = _verify_user(db, store, user_id, event_ids)
    log.info("delete all user=%s existed=%s verified=%s", user_id, report.existed, report.verified)
    if not report.verified:
        raise DeletionIncomplete(report)
    return report

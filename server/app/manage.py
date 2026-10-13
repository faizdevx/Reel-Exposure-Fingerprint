"""Operator CLI. Run inside the api container or the server venv:

    python -m app.manage create-invite [--ttl-hours N]
    python -m app.manage sweep-frames
    python -m app.manage rebuild-fingerprints --assignment-version V [--user-id UUID]
    python -m app.manage replay-fingerprint --pipeline-version FP [--user UUID] \
        [--from DAY] [--to DAY]

The invite code is printed once to stdout and stored only as a SHA-256 hash. Never log it.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db import get_engine
from app.fingerprint.engine import FingerprintConfig
from app.fingerprint.replay import (
    DEFAULT_ABS_TOL,
    DEFAULT_REL_TOL,
    ReplayReport,
    Tolerance,
    replay_user,
    users_with_snapshots,
)
from app.fingerprint.store import rebuild_user, users_with_exposures
from app.frames import FrameStore
from app.models import Invite, Topic
from app.prediction.config import PredictionConfig
from app.prediction.store import rebuild_predictions
from app.security import new_secret, sha256_digest


def create_invite(ttl_hours: int) -> str:
    code = new_secret()
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    with DbSession(get_engine()) as db:
        db.add(Invite(code_sha256=sha256_digest(code), expires_at=expires))
        db.commit()
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("create-invite", help="Create a single-use invite code")
    invite.add_argument("--ttl-hours", type=int, default=None, help="Default: REF_INVITE_TTL_HOURS")
    sub.add_parser("sweep-frames", help="Delete raw frames older than the TTL (for cron)")
    rebuild_cmd = sub.add_parser(
        "rebuild-fingerprints",
        help="Rebuild fingerprints and their session and day snapshots from stored assignments",
    )
    rebuild_cmd.add_argument(
        "--assignment-version",
        required=True,
        help="The pipeline_version of the topic assignments to read (no default: name it)",
    )
    rebuild_cmd.add_argument("--user-id", default=None, help="Only this user (UUID)")
    rebuild_cmd.add_argument(
        "--skip-predictions",
        action="store_true",
        help="Rebuild fingerprints only (predictions are rebuilt by default)",
    )
    replay_cmd = sub.add_parser(
        "replay-fingerprint",
        help="Rebuild fingerprints and snapshots from stored assignments and compare with the "
        "stored snapshots (read-only). Exit 0: all match, 1: a mismatch, 2: could not replay",
    )
    replay_cmd.add_argument(
        "--pipeline-version",
        required=True,
        help="The fingerprint pipeline_version of the stored snapshots (fp1-...)",
    )
    replay_cmd.add_argument("--user", default=None, help="Only this user (UUID); default: all")
    replay_cmd.add_argument(
        "--from", dest="date_from", default=None, help="First UTC day to compare (YYYY-MM-DD)"
    )
    replay_cmd.add_argument(
        "--to", dest="date_to", default=None, help="Last UTC day to compare (YYYY-MM-DD)"
    )
    replay_cmd.add_argument("--rel-tol", type=float, default=DEFAULT_REL_TOL)
    replay_cmd.add_argument("--abs-tol", type=float, default=DEFAULT_ABS_TOL)
    replay_cmd.add_argument("--json", action="store_true", help="Print the reports as JSON")
    args = parser.parse_args(argv)

    if args.command == "replay-fingerprint":
        try:
            only = uuid.UUID(args.user) if args.user else None
            start = date.fromisoformat(args.date_from) if args.date_from else None
            end = date.fromisoformat(args.date_to) if args.date_to else None
            tolerance = Tolerance(rel=args.rel_tol, abs=args.abs_tol)
        except ValueError as error:
            parser.error(str(error))
        return replay_fingerprint(
            args.pipeline_version, only, start, end, tolerance, as_json=args.json
        )
    if args.command == "create-invite":
        ttl = args.ttl_hours if args.ttl_hours is not None else get_settings().invite_ttl_hours
        if ttl < 1:
            parser.error("--ttl-hours must be at least 1")
        print(create_invite(ttl))
        return 0
    if args.command == "sweep-frames":
        settings = get_settings()
        result = FrameStore(settings.frame_store_dir).sweep(settings.frame_ttl_hours * 3600)
        print(f"files_deleted={result.files_deleted} dirs_removed={result.dirs_removed}")
        return 0
    if args.command == "rebuild-fingerprints":
        return rebuild_fingerprints(
            args.assignment_version, args.user_id, predictions=not args.skip_predictions
        )
    return 2


def rebuild_fingerprints(
    assignment_version: str, only_user: str | None, *, predictions: bool = True
) -> int:
    """Replay every user's history. Prints counts and the fingerprint version, never content."""
    config = FingerprintConfig()
    with DbSession(get_engine()) as db:
        # The topics the pipeline has stored (the taxonomy, as `sync_topics` seeded it); `other`
        # is not one of the topics a Reel has to be scored on.
        topic_ids = [t for (t,) in db.execute(select(Topic.id).where(Topic.id != 0))]
        users = [uuid.UUID(only_user)] if only_user else users_with_exposures(db)
        for user_id in users:
            result = rebuild_user(
                db,
                user_id,
                assignment_version=assignment_version,
                topic_ids=topic_ids,
                config=config,
            )
            line = (
                f"user={user_id} snapshots={len(result.snapshots)} "
                f"skipped_reels={len(result.skipped)} fingerprint_version={result.pipeline_version}"
            )
            if predictions:
                rebuilt = rebuild_predictions(
                    db,
                    user_id,
                    assignment_version=assignment_version,
                    fp_config=config,
                    config=PredictionConfig(),
                )
                line += (
                    f" prediction_events={rebuilt.events} prediction_rows={rebuilt.prediction_rows}"
                    f" prediction_version={rebuilt.pipeline_version}"
                )
            db.commit()
            print(line)
    return 0


def _replay_lines(report: ReplayReport) -> list[str]:
    head = (
        f"user={report.user_id} status={report.status}"
        + (f" reason={report.reason}" if report.reason else "")
        + f" compared={report.compared} matched={report.matched}"
        + f" only_in_stored={report.only_in_stored} only_in_replay={report.only_in_replay}"
        + f" values={report.values_compared} max_abs_difference={report.max_abs_difference:.3e}"
        + f" deterministic={report.deterministic}"
    )
    lines = [head]
    for d in report.differences:
        lines.append(
            f"  difference {d.key[0]} {d.key[1]} {d.key[2]} {d.field}: "
            f"stored={d.stored} replayed={d.replayed}"
        )
    if report.differences_total > len(report.differences):
        lines.append(f"  ... and {report.differences_total - len(report.differences)} more")
    return lines


def replay_fingerprint(
    pipeline_version: str,
    only_user: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
    tolerance: Tolerance,
    *,
    as_json: bool = False,
) -> int:
    """Replay and compare; never writes. Prints ids, counts and numbers, never Reel content."""
    with DbSession(get_engine()) as db:
        users = [only_user] if only_user else users_with_snapshots(db, pipeline_version)
        reports = [
            replay_user(
                db,
                user,
                pipeline_version,
                date_from=date_from,
                date_to=date_to,
                tolerance=tolerance,
            )
            for user in users
        ]
        db.rollback()  # nothing was written; end the read transaction
    if as_json:
        print(json.dumps([r.as_dict() for r in reports], indent=1, allow_nan=False))
    else:
        for report in reports:
            for line in _replay_lines(report):
                print(line)
    if not reports:
        print(f"no stored snapshots of version {pipeline_version}", file=sys.stderr)
        return 2
    if any(r.status in ("error", "no_snapshots") for r in reports):
        return 2
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())

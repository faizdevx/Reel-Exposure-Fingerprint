"""`replay-fingerprint`: rebuild from stored assignments and compare with stored snapshots.

Regression tests. Needs PostgreSQL + pgvector (`TEST_DATABASE_URL`).
"""

import copy
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session as DbSession

import app.manage as manage
from app.fingerprint.engine import FingerprintConfig, fingerprint_version
from app.fingerprint.replay import Tolerance, replay_user
from app.fingerprint.store import rebuild_user
from app.models import (
    FingerprintSnapshot,
    Prediction,
    ReelExposure,
    Topic,
    TopicAssignment,
    TopicTransition,
    User,
)
from app.models import Session as MonitoringSession

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
AV = "pv-replay"
TOPICS = (1, 2, 3)
CFG = FingerprintConfig()
SERVER = Path(__file__).resolve().parents[1] / "server"


def fp(config: FingerprintConfig = CFG, av: str = AV) -> str:
    return fingerprint_version(config, av)


def make_user(db: DbSession, email: str) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, password_hash="x")
    db.add(user)
    db.commit()
    return user.id


def seed_topics(db: DbSession) -> None:
    for tid in TOPICS:
        if db.get(Topic, tid) is None:
            db.add(Topic(id=tid, slug=f"t{tid}", label=f"T{tid}", taxonomy_version="t1"))
    db.commit()


def add_reels(
    db: DbSession,
    user_id: uuid.UUID,
    plan: Sequence[tuple[int, float, int]],
    *,
    prefix: str = "r",
    sessions: int = 3,
) -> list[uuid.UUID]:
    """`plan` rows are `(topic, minutes after T0, dwell ms)`; Reels are spread over sessions by
    time order. Returns the session ids."""
    seed_topics(db)
    session_ids = [uuid.uuid4() for _ in range(sessions)]
    for sid in session_ids:
        db.add(
            MonitoringSession(
                id=sid,
                user_id=user_id,
                started_at=T0 - timedelta(days=1),
                ended_at=T0 + timedelta(days=30),
                status="stopped",
                stop_reason="user",
            )
        )
    db.commit()
    for i, (topic, minutes, dwell) in enumerate(plan):
        code = f"{prefix}{i}"
        for tid in TOPICS:
            db.add(
                TopicAssignment(
                    user_id=user_id,
                    reel_shortcode=code,
                    pipeline_version=AV,
                    topic_id=tid,
                    p_fused=0.7 if tid == topic else 0.15,
                )
            )
        db.add(
            ReelExposure(
                event_id=uuid.UUID(int=ord(prefix[0]) * 1000 + i + 1),
                session_id=session_ids[min(i * sessions // len(plan), sessions - 1)],
                user_id=user_id,
                reel_shortcode=code,
                observed_at=T0 + timedelta(minutes=minutes),
                dwell_ms=dwell,
                video_duration_ms=20_000,
                field_status={},
                adapter_version="t",
            )
        )
    db.commit()
    return session_ids


PLAN = [
    (1, 0, 12_000),
    (2, 1, 800),
    (2, 2, 20_000),
    (3, 3, 9_000),
    (1, 3 * 24 * 60, 15_000),
    (1, 3 * 24 * 60 + 1, 3_000),
    (2, 6 * 24 * 60, 18_000),
    (3, 10 * 24 * 60, 5_000),
    (3, 10 * 24 * 60 + 5, 19_000),
    (1, 15 * 24 * 60, 12_000),
]


def built(
    db: DbSession, email: str = "a@example.test", config: FingerprintConfig = CFG
) -> uuid.UUID:
    user = make_user(db, email)
    add_reels(db, user, PLAN, prefix=email[0])
    rebuild_user(db, user, assignment_version=AV, topic_ids=TOPICS, config=config)
    db.commit()
    return user


def snapshots(
    db: DbSession, user: uuid.UUID, version: str | None = None
) -> list[FingerprintSnapshot]:
    db.expire_all()
    q = select(FingerprintSnapshot).where(FingerprintSnapshot.user_id == user)
    if version:
        q = q.where(FingerprintSnapshot.pipeline_version == version)
    return list(db.execute(q.order_by(FingerprintSnapshot.id)).scalars())


def edit_weights(db: DbSession, row: FingerprintSnapshot, edit: Any) -> None:
    weights = copy.deepcopy(row.weights)
    edit(weights)
    db.execute(
        update(FingerprintSnapshot).where(FingerprintSnapshot.id == row.id).values(weights=weights)
    )
    db.commit()


def dump(db: DbSession) -> list[Any]:
    db.expire_all()
    rows: list[Any] = []
    for model in (FingerprintSnapshot, ReelExposure, TopicAssignment, Prediction, TopicTransition):
        rows.append(db.execute(select(model)).all())
    return [[tuple(r) for r in group] for group in rows]


# ------------------------------------------------------------------ matches


def test_a_fresh_rebuild_replays_to_the_same_numbers(db: DbSession) -> None:
    user = built(db)
    report = replay_user(db, user, fp())
    assert report.status == "match" and report.ok and report.reason is None
    assert report.assignment_version == AV
    # 3 sessions (one empty is not a snapshot) and the days, both fingerprints
    assert report.compared == report.stored_snapshots == report.replayed_snapshots
    assert report.compared == len(snapshots(db, user)) > 10
    assert report.matched == report.compared
    assert report.only_in_stored == report.only_in_replay == 0
    assert report.deterministic is True
    assert report.max_abs_difference == 0.0 and report.max_rel_difference == 0.0
    assert report.values_compared > 50


def test_replay_covers_both_fingerprints_session_and_day_snapshots(db: DbSession) -> None:
    user = built(db)
    rows = snapshots(db, user)
    assert {(r.kind, r.bucket) for r in rows} == {
        ("exposure", "session"),
        ("exposure", "day"),
        ("engagement", "session"),
        ("engagement", "day"),
    }
    assert replay_user(db, user, fp()).compared == len(rows)


def test_the_parameters_come_from_the_stored_provenance_not_from_the_defaults(
    db: DbSession,
) -> None:
    config = replace(CFG, half_life_days=3.0, skip_dwell_ms=2_000)
    user = built(db, config=config)
    assert fp(config) != fp(CFG)
    report = replay_user(db, user, fp(config))
    assert report.status == "match" and report.compared > 0
    # the same history replayed under the defaults would not match: the provenance is what decides
    assert replay_user(db, user, fp(CFG)).status == "no_snapshots"


def test_replay_is_read_only(db: DbSession) -> None:
    user = built(db)
    before = dump(db)
    replay_user(db, user, fp())
    edit = snapshots(db, user)[0]
    edit_weights(db, edit, lambda w: w["raw"].__setitem__("1", 42.0))
    changed = dump(db)
    assert changed != before
    report = replay_user(db, user, fp())  # a mismatch is reported, and repaired by nothing
    assert report.status == "mismatch"
    assert dump(db) == changed


def test_two_reels_at_the_same_instant_replay_the_same(db: DbSession) -> None:
    user = make_user(db, "tie@example.test")
    add_reels(db, user, [(1, 0, 9_000), (2, 0, 9_000), (3, 0, 9_000), (1, 5, 9_000)], prefix="t")
    rebuild_user(db, user, assignment_version=AV, topic_ids=TOPICS, config=CFG)
    db.commit()
    assert replay_user(db, user, fp()).status == "match"


def test_a_user_with_unassigned_reels_replays_the_same(db: DbSession) -> None:
    user = built(db)
    sid = db.execute(select(MonitoringSession.id).where(MonitoringSession.user_id == user)).first()
    assert sid is not None
    db.add(
        ReelExposure(
            event_id=uuid.uuid4(),
            session_id=sid[0],
            user_id=user,
            reel_shortcode="no-assignment",
            observed_at=T0 + timedelta(minutes=30),
            dwell_ms=5_000,
            video_duration_ms=10_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()
    rebuild_user(db, user, assignment_version=AV, topic_ids=TOPICS, config=CFG)
    db.commit()
    assert replay_user(db, user, fp()).status == "match"


# ------------------------------------------------------------------ mismatches are found


def test_a_stored_weight_changed_beyond_tolerance_is_a_mismatch_that_names_it(
    db: DbSession,
) -> None:
    user = built(db)
    target = next(r for r in snapshots(db, user) if r.kind == "engagement" and r.bucket == "day")
    edit_weights(db, target, lambda w: w["raw"].__setitem__("2", w["raw"]["2"] * 1.001))
    report = replay_user(db, user, fp())
    assert report.status == "mismatch" and not report.ok
    assert report.differences_total == 1 and report.matched == report.compared - 1
    d = report.differences[0]
    assert (d.key[0], d.key[1], d.field) == ("engagement", "day", "raw[2]")
    assert d.abs_difference and d.abs_difference > 0


def test_a_change_within_tolerance_matches_and_a_tighter_tolerance_finds_it(db: DbSession) -> None:
    user = built(db)
    target = snapshots(db, user)[0]
    edit_weights(db, target, lambda w: w["share"].__setitem__("1", w["share"]["1"] * (1 + 1e-11)))
    assert replay_user(db, user, fp()).status == "match"
    strict = replay_user(db, user, fp(), tolerance=Tolerance(rel=0.0, abs=0.0))
    assert strict.status == "mismatch"


def test_n_reels_and_taken_at_must_match_exactly(db: DbSession) -> None:
    user = built(db)
    rows = snapshots(db, user)
    db.execute(
        update(FingerprintSnapshot)
        .where(FingerprintSnapshot.id == rows[0].id)
        .values(n_reels=rows[0].n_reels + 1)
    )
    db.execute(
        update(FingerprintSnapshot)
        .where(FingerprintSnapshot.id == rows[1].id)
        .values(taken_at=rows[1].taken_at + timedelta(seconds=1))
    )
    db.commit()
    report = replay_user(db, user, fp())
    assert {d.field for d in report.differences} == {"n_reels", "taken_at"}


def test_a_changed_stored_assignment_is_found(db: DbSession) -> None:
    user = built(db)
    for topic, value in ((1, 0.4), (2, 0.45)):  # still sums to 1 with the third at 0.15
        db.execute(
            update(TopicAssignment)
            .where(TopicAssignment.reel_shortcode == "a2", TopicAssignment.topic_id == topic)
            .values(p_fused=value)
        )
    db.commit()
    report = replay_user(db, user, fp())
    assert report.status == "mismatch" and report.differences_total > 0


def test_an_assignment_the_engine_would_refuse_is_an_error_with_a_reason(db: DbSession) -> None:
    user = built(db)
    db.execute(
        update(TopicAssignment)
        .where(TopicAssignment.reel_shortcode == "a2", TopicAssignment.topic_id == 1)
        .values(p_fused=0.4)  # no longer sums to 1
    )
    db.commit()
    report = replay_user(db, user, fp())
    assert (report.status, report.reason) == ("error", "inputs_rejected_by_engine")


def test_a_changed_dwell_is_found(db: DbSession) -> None:
    user = built(db)
    db.execute(update(ReelExposure).where(ReelExposure.reel_shortcode == "a6").values(dwell_ms=100))
    db.commit()
    report = replay_user(db, user, fp())
    assert report.status == "mismatch"
    assert any(d.key[0] == "engagement" for d in report.differences)
    assert not any(d.key[0] == "exposure" and d.field.startswith("raw") for d in report.differences)


def test_a_reel_added_after_the_snapshots_shows_as_new_numbers_and_days(db: DbSession) -> None:
    user = built(db)
    sid = db.execute(select(MonitoringSession.id).where(MonitoringSession.user_id == user)).first()
    assert sid is not None
    for tid in TOPICS:
        db.add(
            TopicAssignment(
                user_id=user,
                reel_shortcode="late",
                pipeline_version=AV,
                topic_id=tid,
                p_fused=1 / 3,
            )
        )
    db.add(
        ReelExposure(
            event_id=uuid.uuid4(),
            session_id=sid[0],
            user_id=user,
            reel_shortcode="late",
            observed_at=T0 + timedelta(days=20),
            dwell_ms=9_000,
            video_duration_ms=10_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()
    report = replay_user(db, user, fp())
    assert report.status == "mismatch" and report.only_in_replay >= 1  # a day with no stored row


def test_a_deleted_reel_is_found(db: DbSession) -> None:
    user = built(db)
    db.execute(delete(ReelExposure).where(ReelExposure.reel_shortcode == "a9"))
    db.commit()
    report = replay_user(db, user, fp())
    assert report.status == "mismatch" and report.only_in_stored >= 1


def test_a_missing_stored_row_and_an_extra_stored_row_are_both_found(db: DbSession) -> None:
    user = built(db)
    rows = snapshots(db, user)
    victim = next(r for r in rows if r.bucket == "day")
    db.execute(delete(FingerprintSnapshot).where(FingerprintSnapshot.id == victim.id))
    ghost = uuid.uuid4()
    db.add(
        MonitoringSession(
            id=ghost,
            user_id=user,
            started_at=T0,
            ended_at=T0 + timedelta(hours=1),
            status="stopped",
            stop_reason="user",
        )
    )
    db.flush()
    template = next(r for r in rows if r.bucket == "session")
    db.add(
        FingerprintSnapshot(
            user_id=user,
            kind="exposure",
            bucket="session",
            taken_at=T0,
            session_id=ghost,
            weights=template.weights,
            n_reels=1,
            pipeline_version=fp(),
        )
    )
    db.commit()
    report = replay_user(db, user, fp())
    assert report.status == "mismatch"
    assert report.only_in_replay == 1 and report.only_in_stored == 1


# ------------------------------------------------------------------ the range


def test_from_and_to_limit_what_is_compared_but_not_what_is_replayed(db: DbSession) -> None:
    user = built(db)
    days = sorted({r.taken_at.date() for r in snapshots(db, user) if r.bucket == "day"})
    early = next(
        r for r in snapshots(db, user) if r.bucket == "day" and r.taken_at.date() == days[0]
    )
    late = next(
        r for r in snapshots(db, user) if r.bucket == "day" and r.taken_at.date() == days[-1]
    )
    edit_weights(db, early, lambda w: w["raw"].__setitem__("1", 99.0))

    assert replay_user(db, user, fp()).status == "mismatch"
    outside = replay_user(db, user, fp(), date_from=days[1])
    assert outside.status == "match" and 0 < outside.compared < len(snapshots(db, user))
    inside = replay_user(db, user, fp(), date_to=days[0])
    assert inside.status == "mismatch"
    only_late = replay_user(
        db, user, fp(), date_from=late.taken_at.date(), date_to=late.taken_at.date()
    )
    assert only_late.status == "match" and only_late.compared >= 2  # that day, both fingerprints
    # a window that starts late still replays from the first Reel: the numbers are the cumulative ones
    assert only_late.max_abs_difference == 0.0


def test_a_window_with_nothing_in_it_compares_nothing_and_says_so(db: DbSession) -> None:
    user = built(db)
    report = replay_user(db, user, fp(), date_from=date(2035, 1, 1))
    assert report.compared == 0 and report.stored_snapshots > 0 and report.status == "match"


def test_from_after_to_is_an_error(db: DbSession) -> None:
    user = built(db)
    report = replay_user(db, user, fp(), date_from=date(2026, 11, 1), date_to=date(2026, 10, 1))
    assert report.status == "error" and report.reason == "from_after_to"


# ------------------------------------------------------------------ cannot replay


def test_nothing_to_compare_for_an_unknown_version_or_user(db: DbSession) -> None:
    user = built(db)
    assert replay_user(db, user, "fp1-0000000000000000").status == "no_snapshots"
    assert replay_user(db, uuid.uuid4(), fp()).status == "no_snapshots"


def test_snapshots_without_provenance_cannot_be_replayed_and_are_not_guessed(db: DbSession) -> None:
    user = built(db)
    for row in snapshots(db, user):
        edit_weights(db, row, lambda w: w.pop("provenance"))
    report = replay_user(db, user, fp())
    assert (report.status, report.reason) == ("error", "no_provenance")


def test_provenance_that_does_not_hash_to_the_version_is_refused(db: DbSession) -> None:
    user = built(db)
    for row in snapshots(db, user):
        edit_weights(
            db, row, lambda w: w["provenance"]["params"].__setitem__("half_life_days", 30.0)
        )
    report = replay_user(db, user, fp())
    assert (report.status, report.reason) == ("error", "provenance_does_not_match_version")


def test_snapshots_that_disagree_about_how_they_were_made_are_refused(db: DbSession) -> None:
    user = built(db)
    edit_weights(
        db, snapshots(db, user)[0], lambda w: w["provenance"].__setitem__("assignment_version", "x")
    )
    assert replay_user(db, user, fp()).reason == "inconsistent_provenance"


def test_unreadable_provenance_is_an_error_not_a_crash(db: DbSession) -> None:
    user = built(db)
    for row in snapshots(db, user):
        edit_weights(db, row, lambda w: w["provenance"].__setitem__("params", {"nonsense": 1}))
    assert replay_user(db, user, fp()).reason == "bad_provenance"


# ------------------------------------------------------------------ users and versions


def test_users_are_replayed_separately(db: DbSession) -> None:
    a, b = built(db, "a@example.test"), built(db, "b@example.test")
    edit_weights(db, snapshots(db, a)[0], lambda w: w["raw"].__setitem__("1", 7.0))
    assert replay_user(db, a, fp()).status == "mismatch"
    assert replay_user(db, b, fp()).status == "match"


def test_another_versions_snapshots_are_neither_compared_nor_touched(db: DbSession) -> None:
    user = built(db)
    other = replace(CFG, half_life_days=14.0)
    rebuild_user(db, user, assignment_version=AV, topic_ids=TOPICS, config=other)
    db.commit()
    old = [(r.id, json.dumps(r.weights, sort_keys=True)) for r in snapshots(db, user, fp(other))]
    report = replay_user(db, user, fp())
    assert report.status == "match" and report.compared == len(snapshots(db, user, fp()))
    assert replay_user(db, user, fp(other)).status == "match"
    assert [
        (r.id, json.dumps(r.weights, sort_keys=True)) for r in snapshots(db, user, fp(other))
    ] == old


def test_a_replay_after_a_second_rebuild_is_still_exact(db: DbSession) -> None:
    user = built(db)
    rebuild_user(db, user, assignment_version=AV, topic_ids=TOPICS, config=CFG)
    db.commit()
    assert replay_user(db, user, fp()).status == "match"


# ------------------------------------------------------------------ the command


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Any:
    monkeypatch.setattr(manage, "get_engine", lambda: migrated_engine)

    def run(*args: str) -> int:
        return manage.main(["replay-fingerprint", *args])

    return run


def test_the_command_exits_zero_when_everything_matches(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    user = built(db)
    assert cli("--pipeline-version", fp(), "--user", str(user)) == 0
    out = capsys.readouterr().out
    assert f"user={user} status=match" in out and "max_abs_difference=0.000e+00" in out
    assert "deterministic=True" in out


def test_the_command_exits_one_on_a_mismatch_and_prints_numbers_only(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    user = built(db)
    target = next(r for r in snapshots(db, user) if r.kind == "exposure" and r.bucket == "day")
    edit_weights(db, target, lambda w: w["raw"].__setitem__("1", 50.0))
    assert cli("--pipeline-version", fp(), "--user", str(user)) == 1
    out = capsys.readouterr().out
    assert (
        "status=mismatch" in out
        and "difference exposure day" in out
        and "raw[1]" in out
        and "stored=50.0" in out
    )
    for forbidden in ("caption", "ocr", "http", "@"):
        assert forbidden not in out.lower()


def test_without_user_it_replays_every_user_of_that_version(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = built(db, "a@example.test"), built(db, "b@example.test")
    edit_weights(db, snapshots(db, b)[0], lambda w: w["raw"].__setitem__("1", 50.0))
    assert cli("--pipeline-version", fp()) == 1
    out = capsys.readouterr().out
    assert f"user={a} status=match" in out and f"user={b} status=mismatch" in out


def test_from_and_to_reach_the_command(db: DbSession, cli: Any) -> None:
    user = built(db)
    first = min(r.taken_at for r in snapshots(db, user)).date()
    edit_weights(
        db,
        next(r for r in snapshots(db, user) if r.taken_at.date() == first),
        lambda w: w["raw"].__setitem__("1", 50.0),
    )
    later = (first + timedelta(days=1)).isoformat()
    assert cli("--pipeline-version", fp(), "--user", str(user), "--from", later) == 0
    assert cli("--pipeline-version", fp(), "--user", str(user), "--to", first.isoformat()) == 1
    assert (
        cli(
            "--pipeline-version",
            fp(),
            "--user",
            str(user),
            "--from",
            first.isoformat(),
            "--to",
            first.isoformat(),
        )
        == 1
    )


def test_the_command_exits_two_when_it_cannot_replay(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    user = built(db)
    assert cli("--pipeline-version", "fp1-0000000000000000") == 2  # nothing stored
    assert "no stored snapshots" in capsys.readouterr().err
    assert cli("--pipeline-version", "fp1-0000000000000000", "--user", str(user)) == 2
    assert (
        cli(
            "--pipeline-version",
            fp(),
            "--user",
            str(user),
            "--from",
            "2026-11-01",
            "--to",
            "2026-10-01",
        )
        == 2
    )


@pytest.mark.parametrize(
    "bad",
    [
        ["--from", "yesterday"],
        ["--to", "2026-13-40"],
        ["--user", "not-a-uuid"],
        ["--rel-tol", "-1"],
    ],
)
def test_bad_arguments_are_refused_before_touching_the_database(cli: Any, bad: list[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli("--pipeline-version", "fp1-x", *bad)
    assert stop.value.code == 2


def test_the_pipeline_version_is_required(cli: Any) -> None:
    with pytest.raises(SystemExit) as stop:
        cli("--user", str(uuid.uuid4()))
    assert stop.value.code == 2


def test_json_output_is_strict_and_complete(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    user = built(db)
    assert cli("--pipeline-version", fp(), "--user", str(user), "--json") == 0
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == 1 and reports[0]["format"] == "fingerprint-replay/1"
    r = reports[0]
    assert r["status"] == "match" and r["deterministic"] is True
    assert r["tolerance"] == {"rel": 1e-9, "abs": 1e-12} and r["matched"] == r["compared"] > 0


def test_a_custom_tolerance_is_used_and_reported(
    db: DbSession, cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    user = built(db)
    edit_weights(
        db, snapshots(db, user)[0], lambda w: w["share"].__setitem__("1", w["share"]["1"] + 1e-6)
    )
    assert cli("--pipeline-version", fp(), "--user", str(user)) == 1
    capsys.readouterr()
    assert (
        cli(
            "--pipeline-version",
            fp(),
            "--user",
            str(user),
            "--abs-tol",
            "1e-5",
            "--rel-tol",
            "1e-5",
            "--json",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["tolerance"] == {"rel": 1e-5, "abs": 1e-5}


def test_the_real_entry_point_gives_identical_output_whatever_the_hash_seed(
    db: DbSession, migrated_engine: Engine
) -> None:
    """The replay must not depend on dict or set iteration order: run it in fresh processes."""
    user = built(db)
    url = migrated_engine.url.render_as_string(hide_password=False)
    outputs = []
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "REF_DATABASE_URL": url}
        done = subprocess.run(  # noqa: S603  (our own interpreter and module, fixed arguments)
            [
                sys.executable,
                "-m",
                "app.manage",
                "replay-fingerprint",
                "--pipeline-version",
                fp(),
                "--user",
                str(user),
                "--json",
            ],
            cwd=SERVER,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-500:]
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert json.loads(outputs[0])[0]["status"] == "match"

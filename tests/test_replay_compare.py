"""Comparing replayed fingerprint snapshots with stored ones (`app.fingerprint.replay`). Pure.

Regression tests for the rule: the same history gives the same snapshots, and any change above the
stated tolerance is found and named.
"""

import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import app.fingerprint.replay as replay_module
from app.fingerprint.engine import FingerprintConfig, Observation, rebuild
from app.fingerprint.replay import (
    DEFAULT_ABS_TOL,
    DEFAULT_REL_TOL,
    ReplayReport,
    StoredSnapshot,
    Tolerance,
    compare_snapshots,
)

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
AV = "av-test"
CFG = FingerprintConfig()
S1, S2 = uuid.UUID(int=1), uuid.UUID(int=2)


def obs(n: int, topic: int, minutes: float, session: uuid.UUID, dwell: int = 12_000) -> Observation:
    return Observation(
        event_id=uuid.UUID(int=1000 + n),
        session_id=session,
        observed_at=T0 + timedelta(minutes=minutes),
        dwell_ms=dwell,
        video_duration_ms=20_000,
        probabilities={1: 0.7 if topic == 1 else 0.2, 2: 0.3 if topic == 1 else 0.8},
    )


def history() -> list[Observation]:
    return [
        obs(0, 1, 0, S1),
        obs(1, 2, 1, S1, dwell=900),
        obs(2, 2, 2, S1),
        obs(3, 1, 3 * 24 * 60, S2),
        obs(4, 2, 3 * 24 * 60 + 1, S2, dwell=20_000),
        obs(5, 1, 9 * 24 * 60, S2),
    ]


def stored_of(observations: list[Observation], config: FingerprintConfig = CFG):  # type: ignore[no-untyped-def]
    result = rebuild(observations, config, AV)
    stored = [
        StoredSnapshot(
            r.kind,
            r.bucket,
            r.taken_at,
            r.session_id,
            json.loads(json.dumps(r.weights)),  # as the jsonb column returns it
            r.n_reels,
        )
        for r in result.snapshots
    ]
    return stored, result.snapshots


def run(stored, replayed, **kw) -> ReplayReport:  # type: ignore[no-untyped-def]
    report = ReplayReport(uuid.UUID(int=7), "fp1-x", "match", **kw)
    compare_snapshots(stored, replayed, report)
    return report


# ------------------------------------------------------------------ tolerance


def test_the_default_tolerance_is_defined_and_documented() -> None:
    assert (Tolerance().rel, Tolerance().abs) == (DEFAULT_REL_TOL, DEFAULT_ABS_TOL) == (1e-9, 1e-12)


@pytest.mark.parametrize(
    ("a", "b", "close"),
    [
        (1.0, 1.0, True),
        (1.0, 1.0 + 5e-10, True),  # inside the relative tolerance
        (1.0, 1.0 + 2e-9, False),  # outside it
        (0.0, 5e-13, True),  # near zero the absolute tolerance rules
        (0.0, 5e-12, False),
        (1e6, 1e6 * (1 + 5e-10), True),  # relative scales with the size
        (float("nan"), float("nan"), False),
        (float("inf"), float("inf"), False),
        (0.5, float("nan"), False),
    ],
)
def test_tolerance_close(a: float, b: float, close: bool) -> None:
    assert Tolerance().close(a, b) is close
    assert Tolerance().close(b, a) is close  # symmetric


def test_tolerances_must_be_sane() -> None:
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="tolerances"):
            Tolerance(rel=bad)
        with pytest.raises(ValueError, match="tolerances"):
            Tolerance(abs=bad)


# ------------------------------------------------------------------ comparing


def test_an_identical_replay_matches_every_snapshot_with_zero_difference() -> None:
    stored, replayed = stored_of(history())
    report = run(stored, replayed)
    # two sessions and three days, for both fingerprints
    assert report.stored_snapshots == report.replayed_snapshots == report.compared == 10
    assert report.matched == 10 and report.differences_total == 0
    assert report.max_abs_difference == 0.0 and report.max_rel_difference == 0.0
    assert report.values_compared == 40  # raw and share of 2 topics, in 10 snapshots


def test_the_replay_is_independent_of_input_order() -> None:
    stored, _ = stored_of(history())
    shuffled = history()
    shuffled.reverse()
    again = rebuild(shuffled, CFG, AV).snapshots
    assert run(stored, again).differences_total == 0


def test_a_hand_computed_case() -> None:
    """Topic 1 at t0, topic 2 seven days later (one half-life): W1 = 0.5, W2 = 1 -> 1/3 and 2/3."""
    a = Observation(uuid.UUID(int=1), S1, T0, 15_000, 15_000, {1: 1.0, 2: 0.0})
    b = Observation(uuid.UUID(int=2), S2, T0 + timedelta(days=7), 15_000, 15_000, {1: 0.0, 2: 1.0})
    snapshots = rebuild([a, b], CFG, AV).snapshots
    last = [s for s in snapshots if s.kind == "exposure" and s.bucket == "day"][-1]
    assert last.weights["raw"] == {"1": pytest.approx(0.5), "2": pytest.approx(1.0)}
    assert last.weights["share"] == {"1": pytest.approx(1 / 3), "2": pytest.approx(2 / 3)}
    stored, replayed = stored_of([a, b])
    assert run(stored, replayed).differences_total == 0


def tampered(stored: list[StoredSnapshot], index: int, edit) -> list[StoredSnapshot]:  # type: ignore[no-untyped-def]
    out = list(stored)
    weights = copy.deepcopy(dict(out[index].weights))
    n_reels = out[index].n_reels
    n_reels = edit(weights, n_reels) or n_reels
    out[index] = replace(out[index], weights=weights, n_reels=n_reels)
    return out


def test_a_weight_changed_beyond_the_tolerance_is_found_and_named() -> None:
    stored, replayed = stored_of(history())
    target = next(i for i, s in enumerate(stored) if s.kind == "exposure" and s.bucket == "day")
    bad = tampered(
        stored, target, lambda w, n: w["raw"].__setitem__("1", w["raw"]["1"] * (1 + 1e-6))
    )
    report = run(bad, replayed)
    assert report.status == "match"  # the status is set by the caller; the findings are here
    assert report.differences_total == 1 and report.matched == report.compared - 1
    d = report.differences[0]
    assert (d.key[0], d.key[1], d.field) == ("exposure", "day", "raw[1]")
    assert d.abs_difference is not None and d.abs_difference > 0
    assert report.max_rel_difference == pytest.approx(1e-6 / (1 + 1e-6), rel=1e-3)


def test_a_change_inside_the_tolerance_matches_but_is_still_measured() -> None:
    stored, replayed = stored_of(history())
    target = next(
        i for i, s in enumerate(stored) if s.kind == "engagement" and s.bucket == "session"
    )
    bad = tampered(
        stored, target, lambda w, n: w["share"].__setitem__("1", w["share"]["1"] * (1 + 1e-11))
    )
    report = run(bad, replayed)
    assert report.differences_total == 0 and report.matched == report.compared
    assert 0.0 < report.max_rel_difference < 1e-9


def test_a_tighter_tolerance_finds_what_the_default_accepts() -> None:
    stored, replayed = stored_of(history())
    bad = tampered(stored, 0, lambda w, n: w["raw"].__setitem__("1", w["raw"]["1"] * (1 + 1e-11)))
    assert run(bad, replayed).differences_total == 0
    strict = ReplayReport(uuid.UUID(int=7), "fp1-x", "match", tolerance=Tolerance(rel=0.0, abs=0.0))
    compare_snapshots(bad, replayed, strict)
    assert strict.differences_total >= 1


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("n_reels", lambda w, n: n + 1),
        ("bucket_reels", lambda w, n: w.__setitem__("bucket_reels", w["bucket_reels"] + 1)),
        (
            "last_observed_at",
            lambda w, n: w.__setitem__("last_observed_at", "2020-01-01T00:00:00+00:00"),
        ),
        ("provenance", lambda w, n: w["provenance"]["params"].__setitem__("half_life_days", 3.0)),
        ("schema", lambda w, n: w.__setitem__("schema", "other/9")),
    ],
)
def test_every_non_weight_field_is_compared(field: str, edit) -> None:  # type: ignore[no-untyped-def]
    stored, replayed = stored_of(history())
    report = run(tampered(stored, 0, edit), replayed)
    assert [d.field for d in report.differences] == [field]


def test_a_topic_present_on_one_side_only_is_a_difference_not_ignored() -> None:
    stored, replayed = stored_of(history())
    bad = tampered(stored, 0, lambda w, n: w["raw"].pop("2"))
    report = run(bad, replayed)
    assert any(d.field == "raw[2]" and d.stored == "absent" for d in report.differences)


def test_nan_in_a_stored_weight_never_matches() -> None:
    stored, replayed = stored_of(history())
    bad = tampered(stored, 0, lambda w, n: w["raw"].__setitem__("1", float("nan")))
    report = run(bad, replayed)
    assert report.differences_total == 1
    json.dumps(report.as_dict(), allow_nan=False)  # and the report stays strict JSON


def test_a_snapshot_only_in_the_replay_or_only_in_the_store_is_a_mismatch() -> None:
    stored, replayed = stored_of(history())
    report = run(stored[:-1], replayed)  # a stored row is missing
    assert report.only_in_replay == 1 and report.differences_total == 1
    report = run(stored, replayed[:-1])  # a Reel was deleted since: the replay has less
    assert report.only_in_stored == 1 and report.differences_total == 1


def test_a_changed_history_changes_the_replay() -> None:
    stored, _ = stored_of(history())
    changed = history()
    changed[2] = replace(changed[2], dwell_ms=100)  # a skip instead of a view
    report = run(stored, rebuild(changed, CFG, AV).snapshots)
    assert report.differences_total > 0
    fields = {d.field for d in report.differences}
    assert any(f.startswith("raw[") for f in fields)


def test_a_different_half_life_is_not_a_match() -> None:
    stored, _ = stored_of(history())
    other = replace(CFG, half_life_days=3.0)
    report = run(stored, rebuild(history(), other, AV).snapshots)
    assert report.differences_total > 0
    assert "provenance" in {d.field for d in report.differences}


# ------------------------------------------------------------------ the range


def test_from_and_to_choose_which_snapshots_are_compared_inclusive() -> None:
    stored, replayed = stored_of(history())
    days = sorted({s.taken_at.date() for s in stored})
    assert len(days) == 3
    only_middle = run(stored, replayed, date_from=days[1], date_to=days[1])
    assert only_middle.compared == 2 and only_middle.differences_total == 0  # that day, both kinds
    everything = run(stored, replayed)
    assert everything.compared == 10
    after = run(stored, replayed, date_from=days[1])
    before = run(stored, replayed, date_to=days[0])
    assert after.compared + before.compared >= 10
    assert run(stored, replayed, date_from=date(2030, 1, 1)).compared == 0


def test_a_change_outside_the_range_is_not_reported_and_inside_it_is() -> None:
    stored, replayed = stored_of(history())
    days = sorted({s.taken_at.date() for s in stored})
    first_day = next(
        i for i, s in enumerate(stored) if s.bucket == "day" and s.taken_at.date() == days[0]
    )
    bad = tampered(stored, first_day, lambda w, n: w["raw"].__setitem__("1", 99.0))
    assert run(bad, replayed, date_from=days[1]).differences_total == 0
    assert run(bad, replayed, date_to=days[0]).differences_total == 1
    assert run(bad, replayed).differences_total == 1


# ------------------------------------------------------------------ the report


def test_the_report_is_strict_json_with_only_ids_counts_and_numbers() -> None:
    stored, replayed = stored_of(history())
    bad = tampered(stored, 0, lambda w, n: w["raw"].__setitem__("1", 5.0))
    report = run(bad, replayed)
    report.status = "mismatch"
    text = json.dumps(report.as_dict(), allow_nan=False)
    document = json.loads(text)
    assert document["format"] == "fingerprint-replay/1"
    assert document["tolerance"] == {"rel": 1e-9, "abs": 1e-12}
    assert document["differences_total"] == 1 and len(document["differences"]) == 1


def test_the_details_are_capped_but_the_count_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    stored, replayed = stored_of(history())
    wrecked = [replace(s, n_reels=s.n_reels + 1) for s in stored]
    report = run(wrecked, replayed)
    assert report.differences_total == 10
    monkeypatch.setattr(replay_module, "MAX_DETAILS", 3)
    many = run(wrecked, replayed)
    assert many.differences_total == 10 and len(many.differences) == 3

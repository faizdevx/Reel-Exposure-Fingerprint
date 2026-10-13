"""Fingerprint maths (spec §5.2). Pure: no database, no model, no clock.

Expected values are worked out by hand in the comments. Half-life is 7 days unless stated, so
7 days halves a weight, 14 days quarters it, and these are exact in binary floating point.
"""

import json
import math
import random
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.fingerprint.engine import (
    FingerprintConfig,
    FingerprintError,
    FingerprintState,
    Observation,
    OutOfOrderError,
    apply_observation,
    decay_factor,
    engagement_gain,
    fingerprint_version,
    rebuild,
    state_from_document,
    weights_document,
)

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
CFG = FingerprintConfig()
A, B, C = 1, 2, 3
SESSION = uuid.UUID(int=1)


def obs(
    n: int,
    at: datetime,
    probabilities: dict[int, float] | None,
    *,
    dwell_ms: int | None = 10_000,
    duration_ms: int | None = 20_000,
    session: uuid.UUID = SESSION,
) -> Observation:
    return Observation(uuid.UUID(int=1000 + n), session, at, dwell_ms, duration_ms, probabilities)


def days(n: float) -> timedelta:
    return timedelta(days=n)


# ---- gains


def test_exposure_gain_is_one_whatever_the_dwell() -> None:
    state = FingerprintState("exposure")
    for dwell in (0, 500, 10_000, 600_000, None):
        out = apply_observation(state, obs(1, T0, {A: 1.0}, dwell_ms=dwell), CFG)
        assert out.state.weights == {A: 1.0}


@pytest.mark.parametrize(
    ("dwell_ms", "duration_ms", "expected"),
    [
        (10_000, 20_000, 0.5),  # 10 / 20
        (20_000, 20_000, 1.0),  # watched it all
        (30_000, 15_000, 2.0),  # two loops, exactly the cap
        (60_000, 15_000, 2.0),  # 4.0 clipped to 2.0
        (3_000, 2_000, 0.6),  # short Reel: denominator is the 5 s floor, 3 / 5
        (3_000, None, 0.6),  # unknown duration: the same 5 s floor
        (1_500, 5_000, 0.3),  # exactly 1.5 s is NOT a skip: 1.5 / 5
        (1_500, 100_000, 0.1),  # 0.015 clipped up to the minimum
        (4_000, 100_000, 0.1),  # 0.04 clipped up
    ],
)
def test_engagement_gain_formula(dwell_ms: int, duration_ms: int | None, expected: float) -> None:
    assert engagement_gain(dwell_ms, duration_ms, CFG) == pytest.approx(expected, abs=1e-15)


@pytest.mark.parametrize("dwell_ms", [0, 1, 1000, 1499])
def test_sub_1_5_second_dwell_is_a_skip_with_gain_point_one(dwell_ms: int) -> None:
    # Even for a 1 s Reel, where the ratio would be 1000 / 5000 = 0.2, a skip is 0.1.
    assert engagement_gain(dwell_ms, 1_000, CFG) == 0.1


def test_unknown_dwell_has_no_gain_and_bad_input_is_rejected() -> None:
    assert engagement_gain(None, 20_000, CFG) is None
    with pytest.raises(FingerprintError):
        engagement_gain(-1, 20_000, CFG)
    with pytest.raises(FingerprintError):
        engagement_gain(5_000, -1, CFG)


# ---- one Reel


def test_one_reel_exposure_weights_equal_the_distribution() -> None:
    p = {A: 0.5, B: 0.3, C: 0.2}
    out = apply_observation(FingerprintState("exposure"), obs(1, T0, p), CFG)
    assert out.skipped is None
    assert out.state.weights == p
    assert out.state.n_reels == 1
    assert out.state.last_observed_at == T0
    assert out.state.share() == pytest.approx(p)


def test_one_reel_engagement_scales_weights_but_not_the_displayed_share() -> None:
    p = {A: 0.75, B: 0.25}
    out = apply_observation(FingerprintState("engagement"), obs(1, T0, p), CFG)  # g = 0.5
    assert out.state.weights == {A: 0.375, B: 0.125}
    assert sum(out.state.share().values()) == pytest.approx(1.0, abs=1e-15)
    assert out.state.share() == pytest.approx(p)  # one Reel: W / sum(W) cancels g


def test_empty_state_has_no_share() -> None:
    assert FingerprintState("exposure").share() == {}


# ---- decay


def test_decay_factor_at_half_life_multiples() -> None:
    assert decay_factor(0.0, CFG) == 1.0
    assert decay_factor(7 * 86400.0, CFG) == 0.5
    assert decay_factor(14 * 86400.0, CFG) == 0.25
    assert decay_factor(3.5 * 86400.0, CFG) == pytest.approx(2**-0.5, abs=1e-15)
    short = FingerprintConfig(half_life_days=1.0)
    assert decay_factor(86400.0, short) == 0.5


def test_two_reels_seven_days_apart_the_first_halves() -> None:
    # W_A = 1 * 2^-1 = 0.5; W_B = 1. Share: A = 0.5 / 1.5, B = 1 / 1.5.
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG)
    r2 = apply_observation(r1.state, obs(2, T0 + days(7), {B: 1.0}), CFG)
    assert r2.state.weights == {A: 0.5, B: 1.0}
    assert r2.state.share() == pytest.approx({A: 1 / 3, B: 2 / 3})


def test_same_instant_means_no_decay() -> None:
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG)
    r2 = apply_observation(r1.state, obs(2, T0, {A: 1.0}), CFG)
    assert r2.state.weights == {A: 2.0}


def test_decay_composes_across_an_idle_gap() -> None:
    # Reel at 0, at 7 d, at 14 d, all topic A, g = 1: ((1 / 2) + 1) / 2 + 1 = 1.75.
    state = FingerprintState("exposure")
    for i in range(3):
        state = apply_observation(state, obs(i, T0 + days(7 * i), {A: 1.0}), CFG).state
    assert state.weights[A] == 1.75


def test_long_idle_gap_shrinks_old_weights_toward_zero_but_not_below() -> None:
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG)
    r2 = apply_observation(r1.state, obs(2, T0 + days(700), {B: 1.0}), CFG)
    assert 0.0 < r2.state.weights[A] < 1e-29  # 2^-100
    assert r2.state.weights[B] == 1.0


def test_longer_half_life_forgets_slower() -> None:
    slow = FingerprintConfig(half_life_days=14.0)
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), slow)
    r2 = apply_observation(r1.state, obs(2, T0 + days(14), {B: 1.0}), slow)
    assert r2.state.weights[A] == 0.5


def test_dt_uses_observed_at_so_utc_offset_does_not_matter() -> None:
    from datetime import timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG)
    later_in_ist = (T0 + days(7)).astimezone(ist)
    r2 = apply_observation(r1.state, obs(2, later_in_ist, {A: 1.0}), CFG)
    assert r2.state.weights[A] == 1.5


# ---- multiple Reels, engagement versus exposure


def test_multiple_reels_exposure_and_engagement_diverge() -> None:
    # Reel 1 topic A, dwell 20 s of 20 s (g = 1.0). Reel 2 topic B, dwell 1 s (skip, g = 0.1).
    # Both 1 s apart, so decay is 2^(-1/604800), applied below to the first Reel only.
    watched = obs(1, T0, {A: 1.0}, dwell_ms=20_000, duration_ms=20_000)
    skipped = obs(2, T0 + timedelta(seconds=1), {B: 1.0}, dwell_ms=1_000, duration_ms=20_000)
    result = rebuild([watched, skipped], CFG, "av1")
    exposure, engagement = result.states["exposure"], result.states["engagement"]
    f = 2.0 ** (-1 / 604800)
    assert exposure.weights == pytest.approx({A: f, B: 1.0}, abs=1e-15)
    assert engagement.weights == pytest.approx({A: 1.0 * f, B: 0.1}, abs=1e-15)
    # Exposure says the feed was half and half; engagement says the user stayed on A.
    assert exposure.share()[A] == pytest.approx(0.5, abs=1e-5)
    assert engagement.share()[A] == pytest.approx(f / (f + 0.1), abs=1e-12)
    assert engagement.share()[A] > 0.9


def test_skipped_reel_adds_a_tenth_of_its_distribution() -> None:
    p = {A: 0.6, B: 0.4}
    out = apply_observation(
        FingerprintState("engagement"), obs(1, T0, p, dwell_ms=900, duration_ms=30_000), CFG
    )
    assert out.state.weights == pytest.approx({A: 0.06, B: 0.04}, abs=1e-15)
    assert out.state.n_reels == 1  # a skip is still an exposure the user saw


def test_long_dwell_is_capped_at_twice_a_full_view() -> None:
    p = {A: 1.0}
    capped = apply_observation(
        FingerprintState("engagement"), obs(1, T0, p, dwell_ms=300_000, duration_ms=10_000), CFG
    )
    exactly = apply_observation(
        FingerprintState("engagement"), obs(1, T0, p, dwell_ms=20_000, duration_ms=10_000), CFG
    )
    assert capped.state.weights == {A: 2.0}
    assert capped.state.weights == exactly.state.weights


def test_weights_are_not_a_centroid_two_topics_stay_separate() -> None:
    # A centroid of "football" and "AI" would be a point that is neither. Weights keep both.
    r1 = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG)
    r2 = apply_observation(r1.state, obs(2, T0, {B: 1.0}), CFG)
    assert r2.state.weights == {A: 1.0, B: 1.0}
    assert r2.state.share() == {A: 0.5, B: 0.5}


# ---- missing modalities and missing data


def test_reel_without_assignment_changes_nothing_in_either_fingerprint() -> None:
    first = obs(1, T0, {A: 1.0})
    nothing = obs(2, T0 + days(3), None)
    result = rebuild([first, nothing], CFG, "av1")
    for kind in ("exposure", "engagement"):
        state = result.states[kind]
        assert state.weights == ({A: 1.0} if kind == "exposure" else {A: 0.5})
        assert state.n_reels == 1
        assert state.last_observed_at == T0  # the skipped Reel did not move the clock
    assert {(s.event_id, s.kind, s.reason) for s in result.skipped} == {
        (nothing.event_id, "exposure", "no_assignment"),
        (nothing.event_id, "engagement", "no_assignment"),
    }


def test_skipped_reel_does_not_change_the_result_of_later_decay() -> None:
    with_gap = rebuild(
        [obs(1, T0, {A: 1.0}), obs(2, T0 + days(3), None), obs(3, T0 + days(7), {B: 1.0})],
        CFG,
        "av1",
    )
    without = rebuild([obs(1, T0, {A: 1.0}), obs(3, T0 + days(7), {B: 1.0})], CFG, "av1")
    assert with_gap.states["exposure"].weights == without.states["exposure"].weights


def test_unknown_dwell_skips_engagement_only() -> None:
    result = rebuild([obs(1, T0, {A: 1.0}, dwell_ms=None)], CFG, "av1")
    assert result.states["exposure"].weights == {A: 1.0}
    assert result.states["engagement"].weights == {}
    assert result.states["engagement"].share() == {}
    assert [(s.kind, s.reason) for s in result.skipped] == [("engagement", "no_dwell")]


def test_text_only_distribution_is_enough() -> None:
    # Visual missing: the fused distribution is the text one. The engine does not care which
    # modalities made it; a valid distribution is all it needs.
    text_only = {A: 0.9, B: 0.05, C: 0.05}
    out = apply_observation(FingerprintState("exposure"), obs(1, T0, text_only), CFG)
    assert out.state.weights == text_only


def test_zero_probability_topics_are_kept_as_zero_weights() -> None:
    out = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0, B: 0.0}), CFG)
    assert out.state.weights == {A: 1.0, B: 0.0}


@pytest.mark.parametrize(
    "bad",
    [{}, {A: 0.5}, {A: 1.2, B: -0.2}, {A: math.nan, B: 1.0}, {A: math.inf}, {A: 0.6, B: 0.6}],
)
def test_invalid_distributions_are_rejected_not_repaired(bad: dict[int, float]) -> None:
    with pytest.raises(FingerprintError):
        apply_observation(FingerprintState("exposure"), obs(1, T0, bad), CFG)


def test_naive_timestamp_is_rejected() -> None:
    naive = obs(1, datetime(2026, 10, 1, 12, 0, 0), {A: 1.0})
    with pytest.raises(FingerprintError):
        apply_observation(FingerprintState("exposure"), naive, CFG)


def test_older_observation_than_the_state_needs_a_replay() -> None:
    state = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 1.0}), CFG).state
    with pytest.raises(OutOfOrderError):
        apply_observation(state, obs(2, T0 - timedelta(seconds=1), {A: 1.0}), CFG)


# ---- determinism, reproducibility, versions


def _history() -> list[Observation]:
    rng = random.Random(7)
    s1, s2, s3 = (uuid.UUID(int=i) for i in (11, 12, 13))
    out: list[Observation] = []
    for i in range(60):
        raw = [rng.random() for _ in range(3)]
        total = sum(raw)
        dist: dict[int, float] | None = {t: v / total for t, v in zip((A, B, C), raw, strict=True)}
        if i % 11 == 0:
            dist = None
        dwell = None if i % 13 == 0 else rng.randrange(0, 40_000)
        out.append(
            obs(
                i,
                T0 + timedelta(hours=7 * i, seconds=i),
                dist,
                dwell_ms=dwell,
                duration_ms=rng.choice([None, 8_000, 20_000]),
                session=(s1, s2, s3)[i // 20],
            )
        )
    return out


def test_rebuild_is_independent_of_input_order() -> None:
    history = _history()
    shuffled = history[:]
    random.Random(3).shuffle(shuffled)
    assert rebuild(history, CFG, "av1") == rebuild(shuffled, CFG, "av1")


def test_rebuild_twice_is_bit_identical_including_the_stored_json() -> None:
    first = rebuild(_history(), CFG, "av1")
    second = rebuild(_history(), CFG, "av1")
    assert first == second
    dump = [json.dumps(s.weights, sort_keys=True) for s in first.snapshots]
    assert dump == [json.dumps(s.weights, sort_keys=True) for s in second.snapshots]


def test_same_instant_ties_break_by_event_id() -> None:
    # Two Reels at the same instant: the lower event_id is applied first, whatever the input order.
    a, b = obs(1, T0, {A: 1.0}), obs(2, T0, {B: 1.0})
    assert rebuild([a, b], CFG, "av1") == rebuild([b, a], CFG, "av1")


def test_duplicate_event_id_is_rejected() -> None:
    with pytest.raises(FingerprintError):
        rebuild([obs(1, T0, {A: 1.0}), obs(1, T0 + days(1), {B: 1.0})], CFG, "av1")


def test_pipeline_version_is_stable_and_tracks_every_parameter() -> None:
    base = fingerprint_version(CFG, "av1")
    assert base == fingerprint_version(FingerprintConfig(), "av1")
    assert base.startswith("fp1-")
    variants = [
        FingerprintConfig(half_life_days=3.0),
        FingerprintConfig(half_life_days=14.0),
        FingerprintConfig(max_gain=3.0),
        FingerprintConfig(min_gain=0.2),
        FingerprintConfig(min_duration_ms=4000),
        FingerprintConfig(skip_dwell_ms=1000),
        FingerprintConfig(skip_gain=0.05),
    ]
    versions = {fingerprint_version(v, "av1") for v in variants} | {base}
    assert len(versions) == len(variants) + 1
    assert fingerprint_version(CFG, "av2") != base  # upstream assignment version is part of it


def test_every_snapshot_carries_the_pipeline_version() -> None:
    result = rebuild(_history(), CFG, "av1")
    assert result.pipeline_version == fingerprint_version(CFG, "av1")
    assert {s.pipeline_version for s in result.snapshots} == {result.pipeline_version}


def test_invalid_config_is_rejected() -> None:
    for bad in (
        {"half_life_days": 0.0},
        {"half_life_days": -1.0},
        {"half_life_days": math.inf},
        {"min_gain": 0.0},
        {"min_gain": 3.0},
        {"min_duration_ms": 0},
    ):
        with pytest.raises(ValueError):
            FingerprintConfig(**bad)  # type: ignore[arg-type]


# ---- session snapshots (and, below, day snapshots)


def test_one_snapshot_per_session_and_kind_taken_at_the_last_reel() -> None:
    result = rebuild(_history(), CFG, "av1")
    sessions = [s for s in result.snapshots if s.bucket == "session"]
    assert len(sessions) == 3 * 2
    by_session: dict[uuid.UUID | None, list[datetime]] = {}
    for snap in sessions:
        by_session.setdefault(snap.session_id, []).append(snap.taken_at)
    history = _history()
    for index, session in enumerate(uuid.UUID(int=i) for i in (11, 12, 13)):
        last = max(o.observed_at for o in history[index * 20 : (index + 1) * 20])
        assert by_session[session] == [last, last]


def test_snapshot_holds_raw_weights_share_and_resumes_to_the_same_final_state() -> None:
    history = _history()
    full = rebuild(history, CFG, "av1")
    middle = next(
        s for s in full.snapshots if s.session_id == uuid.UUID(int=12) and s.kind == "exposure"
    )
    doc = middle.weights
    assert sum(doc["share"].values()) == pytest.approx(1.0, abs=1e-12)
    assert doc["raw"].keys() == doc["share"].keys()

    # Resume from the snapshot taken after session 2 and apply session 3 only.
    state = state_from_document("exposure", json.loads(json.dumps(doc)), middle.n_reels)
    for o in sorted(history[40:], key=lambda x: (x.observed_at, str(x.event_id))):
        state = apply_observation(state, o, CFG).state
    assert state.weights == full.states["exposure"].weights


def test_session_with_only_skipped_reels_still_gets_an_empty_snapshot() -> None:
    other = uuid.UUID(int=99)
    result = rebuild([obs(1, T0, None, session=other)], CFG, "av1")
    sessions = [s for s in result.snapshots if s.bucket == "session"]
    assert len(sessions) == 2
    assert all(s.n_reels == 0 and s.weights["raw"] == {} for s in sessions)


def test_no_observations_gives_empty_states_and_no_snapshots() -> None:
    result = rebuild([], CFG, "av1")
    assert result.snapshots == ()
    assert all(s.weights == {} and s.n_reels == 0 for s in result.states.values())


# ---- day snapshots (M5)


def utc_day(moment: datetime) -> object:
    return moment.astimezone(UTC).date()


def days_of(history: list[Observation]) -> dict[object, list[Observation]]:
    out: dict[object, list[Observation]] = {}
    for o in sorted(history, key=lambda x: (x.observed_at, str(x.event_id))):
        out.setdefault(utc_day(o.observed_at), []).append(o)
    return out


def test_one_snapshot_per_utc_day_and_kind_taken_at_the_last_reel_of_that_day() -> None:
    history = _history()
    result = rebuild(history, CFG, "av1")
    day_snaps = [s for s in result.snapshots if s.bucket == "day"]
    per_day = days_of(history)
    assert len(day_snaps) == 2 * len(per_day)
    for snap in day_snaps:
        assert snap.session_id is None
    for day, reels in per_day.items():
        mine = [s for s in day_snaps if utc_day(s.taken_at) == day]
        assert sorted(s.kind for s in mine) == ["engagement", "exposure"]
        assert {s.taken_at for s in mine} == {max(o.observed_at for o in reels)}


def test_a_day_snapshot_is_exactly_the_fingerprint_after_that_days_last_reel() -> None:
    """Prefix property: rebuilding only the history up to that Reel gives the same weights."""
    history = _history()
    ordered = sorted(history, key=lambda o: (o.observed_at, str(o.event_id)))
    full = rebuild(history, CFG, "av1")
    for snap in [s for s in full.snapshots if s.bucket == "day"][::5]:  # a spread of days
        prefix = [o for o in ordered if o.observed_at <= snap.taken_at]
        upto = rebuild(prefix, CFG, "av1")
        stored = {k: v for k, v in snap.weights.items() if k not in ("bucket_reels", "provenance")}
        assert weights_document(upto.states[snap.kind]) == stored
        assert upto.states[snap.kind].n_reels == snap.n_reels


def test_the_day_snapshot_counts_every_reel_up_to_that_day() -> None:
    history = [obs(i, T0 + days(i), {A: 1.0}) for i in range(5)]
    day_snaps = [s for s in rebuild(history, CFG, "av1").snapshots if s.bucket == "day"]
    exposure = [s.n_reels for s in day_snaps if s.kind == "exposure"]
    assert exposure == [1, 2, 3, 4, 5]  # cumulative, not per day


def test_a_day_without_a_reel_has_no_snapshot() -> None:
    history = [obs(1, T0, {A: 1.0}), obs(2, T0 + days(3), {B: 1.0})]
    day_snaps = [s for s in rebuild(history, CFG, "av1").snapshots if s.bucket == "day"]
    assert sorted({utc_day(s.taken_at) for s in day_snaps}) == [
        utc_day(T0),
        utc_day(T0 + days(3)),
    ]


def test_midnight_utc_splits_the_days_and_offsets_are_converted() -> None:
    late = datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
    early = datetime(2026, 10, 2, 0, 0, 1, tzinfo=UTC)
    ist = timezone(timedelta(hours=5, minutes=30))
    # 02:00 on 2 Oct in IST is 20:30 on 1 Oct UTC: the first UTC day.
    history = [
        obs(1, late, {A: 1.0}),
        obs(2, early, {B: 1.0}),
        obs(3, datetime(2026, 10, 2, 2, 0, tzinfo=ist), {C: 1.0}),
    ]
    day_snaps = [
        s
        for s in rebuild(history, CFG, "av1").snapshots
        if s.bucket == "day" and s.kind == "exposure"
    ]
    assert [(utc_day(s.taken_at).isoformat(), s.n_reels) for s in day_snaps] == [  # type: ignore[attr-defined]
        ("2026-10-01", 2),  # the 20:30 UTC Reel and the 23:59:59 one
        ("2026-10-02", 3),
    ]


def test_idle_time_does_not_change_the_shares_so_a_gap_needs_no_snapshot() -> None:
    state = apply_observation(FingerprintState("exposure"), obs(1, T0, {A: 0.7, B: 0.3}), CFG).state
    factor = decay_factor(30 * 86400.0, CFG)
    decayed = {k: v * factor for k, v in state.weights.items()}
    total = sum(decayed.values())
    assert {k: v / total for k, v in decayed.items()} == pytest.approx(state.share())


def test_a_session_across_midnight_has_one_session_snapshot_and_two_day_snapshots() -> None:
    session = uuid.UUID(int=500)
    history = [
        obs(1, datetime(2026, 10, 1, 23, 50, tzinfo=UTC), {A: 1.0}, session=session),
        obs(2, datetime(2026, 10, 2, 0, 10, tzinfo=UTC), {B: 1.0}, session=session),
    ]
    snaps = rebuild(history, CFG, "av1").snapshots
    assert sum(s.bucket == "session" and s.kind == "exposure" for s in snaps) == 1
    assert sum(s.bucket == "day" and s.kind == "exposure" for s in snaps) == 2


# ---- time ordering


def test_snapshots_come_out_in_time_order_whatever_order_the_input_had() -> None:
    history = _history()
    shuffled = history[:]
    random.Random(9).shuffle(shuffled)
    result = rebuild(shuffled, CFG, "av1")
    times = [s.taken_at for s in result.snapshots]
    assert times == sorted(times)
    assert result.snapshots == rebuild(history, CFG, "av1").snapshots


def test_within_one_instant_session_snapshots_precede_day_snapshots_and_kinds_are_in_order() -> (
    None
):
    snaps = rebuild([obs(1, T0, {A: 1.0})], CFG, "av1").snapshots
    assert [(s.bucket, s.kind) for s in snaps] == [
        ("session", "exposure"),
        ("session", "engagement"),
        ("day", "exposure"),
        ("day", "engagement"),
    ]


def test_time_never_runs_backwards_in_the_weights_n_reels_of_a_kind() -> None:
    result = rebuild(_history(), CFG, "av1")
    for bucket in ("session", "day"):
        for kind in ("exposure", "engagement"):
            counts = [s.n_reels for s in result.snapshots if s.bucket == bucket and s.kind == kind]
            assert counts == sorted(counts)


# ---- reproducibility


def test_rebuilding_gives_the_same_snapshots_down_to_the_json_text() -> None:
    first = rebuild(_history(), CFG, "av1")
    second = rebuild(_history(), CFG, "av1")
    assert first == second
    dump = lambda r: json.dumps(  # noqa: E731
        [
            (s.bucket, s.kind, s.taken_at.isoformat(), str(s.session_id), s.n_reels, s.weights)
            for s in r.snapshots
        ],
        sort_keys=True,
    )
    assert dump(first) == dump(second)


def test_every_snapshot_of_a_rebuild_carries_the_same_pipeline_version() -> None:
    result = rebuild(_history(), CFG, "av1")
    assert {s.pipeline_version for s in result.snapshots} == {fingerprint_version(CFG, "av1")}


def test_a_changed_half_life_changes_the_version_and_the_weights_but_not_the_time_axis() -> None:
    base = rebuild(_history(), CFG, "av1")
    other = rebuild(_history(), FingerprintConfig(half_life_days=2.0), "av1")
    assert base.pipeline_version != other.pipeline_version
    assert [(s.bucket, s.kind, s.taken_at) for s in base.snapshots] == [
        (s.bucket, s.kind, s.taken_at) for s in other.snapshots
    ]
    assert [s.weights for s in base.snapshots] != [s.weights for s in other.snapshots]


def test_a_new_upstream_assignment_version_is_a_new_fingerprint_version() -> None:
    a, b = rebuild(_history(), CFG, "av1"), rebuild(_history(), CFG, "av2")
    assert a.pipeline_version != b.pipeline_version
    strip = lambda r: [  # noqa: E731
        {k: v for k, v in s.weights.items() if k != "provenance"} for s in r.snapshots
    ]
    assert strip(a) == strip(b)  # same maths, only the recorded provenance differs
    assert a.snapshots[0].weights["provenance"]["assignment_version"] == "av1"
    assert b.snapshots[0].weights["provenance"]["assignment_version"] == "av2"


# ---- reels counted inside a session or a day (not the running total)


def test_each_snapshot_counts_the_reels_of_its_own_session_and_day() -> None:
    history = _history()
    result = rebuild(history, CFG, "av1")
    for kind in ("exposure", "engagement"):
        sessions = [s for s in result.snapshots if s.bucket == "session" and s.kind == kind]
        days_ = [s for s in result.snapshots if s.bucket == "day" and s.kind == kind]
        assert sum(s.weights["bucket_reels"] for s in sessions) == result.states[kind].n_reels
        assert sum(s.weights["bucket_reels"] for s in days_) == result.states[kind].n_reels
        running = 0
        for snap in sessions:
            running += snap.weights["bucket_reels"]
            assert running == snap.n_reels  # the row's n_reels is the running total


def test_skipped_reels_are_not_counted_in_a_bucket() -> None:
    session = uuid.UUID(int=700)
    history = [
        obs(1, T0, {A: 1.0}, session=session),
        obs(2, T0 + timedelta(minutes=1), None, session=session),  # no assignment
        obs(3, T0 + timedelta(minutes=2), {B: 1.0}, dwell_ms=None, session=session),  # no dwell
    ]
    snaps = rebuild(history, CFG, "av1").snapshots
    by_kind = {s.kind: s.weights["bucket_reels"] for s in snaps if s.bucket == "session"}
    assert by_kind == {"exposure": 2, "engagement": 1}

"""Topic evidence: GET /api/v1/topics/{id}/evidence. Needs a database.

The central check is reconciliation: the contributions listed for a topic add up to the weight the
stored snapshot has for it, so the list really is the reason the topic is there.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor

from app.fingerprint.engine import FingerprintConfig, engagement_gain
from app.fingerprint.evidence import CAPTION_MAX_CHARS, python_contribution
from app.fingerprint.store import rebuild_user
from app.models import FingerprintSnapshot, ReelExposure, Topic, TopicAssignment, User
from app.models import Session as MonitoringSession
from app.worker.fusion import OTHER_TOPIC_ID

pytestmark = pytest.mark.db

API = "/api/v1"
AV = "pv-evidence"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
TOPICS = (1, 2, 3)
CFG = FingerprintConfig()


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def seed_topics(db: Session) -> None:
    for tid, slug in ((1, "cricket"), (2, "tech"), (3, "food")):
        if db.get(Topic, tid) is None:
            db.add(Topic(id=tid, slug=slug, label=slug.title(), taxonomy_version="t1"))
    if db.get(Topic, OTHER_TOPIC_ID) is None:
        db.add(Topic(id=OTHER_TOPIC_ID, slug="other", label="Other", taxonomy_version="t1"))
    db.commit()


_PENDING: dict[tuple[str, str], dict[int, float]] = {}


@pytest.fixture(autouse=True)
def _reset_pending() -> None:
    _PENDING.clear()


def assign(db: Session, shortcode: str, probs: dict[int, float], *, version: str = AV) -> None:
    """Say what the pipeline would assign to a Reel. Rows are per user (ADR-024), so they are
    written for each user when that user watches the Reel (`watch`)."""
    _PENDING[(shortcode, version)] = dict(probs)


def materialize(db: Session, user_id: str, shortcode: str) -> None:
    owner = uuid.UUID(user_id)
    for (code, version), probs in _PENDING.items():
        if code != shortcode:
            continue
        for tid in TOPICS:
            if db.get(TopicAssignment, (owner, code, version, tid)) is None:
                db.add(
                    TopicAssignment(
                        user_id=owner,
                        reel_shortcode=code,
                        pipeline_version=version,
                        topic_id=tid,
                        p_text=probs[tid],
                        p_visual=None,
                        p_fused=probs[tid],
                    )
                )
    db.commit()


def new_session(db: Session, user_id: str, at: datetime) -> uuid.UUID:
    sid = uuid.uuid4()
    db.add(
        MonitoringSession(
            id=sid,
            user_id=uuid.UUID(user_id),
            started_at=at,
            ended_at=at + timedelta(minutes=30),
            status="stopped",
            stop_reason="user",
        )
    )
    db.commit()
    return sid


def watch(
    db: Session,
    user_id: str,
    session_id: uuid.UUID,
    shortcode: str,
    at: datetime,
    *,
    dwell_ms: int | None = 10_000,
    duration_ms: int | None = 20_000,
    caption: str | None = "a caption",
) -> uuid.UUID:
    event_id = uuid.uuid4()
    db.add(
        ReelExposure(
            event_id=event_id,
            session_id=session_id,
            user_id=uuid.UUID(user_id),
            reel_shortcode=shortcode,
            observed_at=at,
            dwell_ms=dwell_ms,
            video_duration_ms=duration_ms,
            caption=caption,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()
    materialize(db, user_id, shortcode)
    return event_id


def rebuild(db: Session, user_id: str) -> None:
    rebuild_user(db, uuid.UUID(user_id), assignment_version=AV, topic_ids=TOPICS, config=CFG)
    db.commit()


def evidence(actor: Actor, topic: int, kind: str = "exposure", **params: object) -> dict:  # type: ignore[type-arg]
    query = "&".join(f"{k}={v}" for k, v in {"kind": kind, **params}.items())
    response = actor.dashboard.get(f"{API}/topics/{topic}/evidence?{query}")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def snapshot_weight(db: Session, user_id: str, kind: str, topic: int) -> float:
    db.expire_all()
    snap = (
        db.query(FingerprintSnapshot)
        .filter(FingerprintSnapshot.user_id == uuid.UUID(user_id), FingerprintSnapshot.kind == kind)
        .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
        .first()
    )
    assert snap is not None
    return float(snap.weights["raw"][str(topic)])


def populate(db: Session, actor: Actor, reels: int = 30) -> list[uuid.UUID]:
    """A user with `reels` Reels over several days; some skipped by the fingerprint."""
    seed_topics(db)
    ids: list[uuid.UUID] = []
    session = new_session(db, actor.user_id, T0)
    for i in range(reels):
        code = f"reel{i:03d}"
        lean = i % 3
        probs = {t: (0.8 if t == TOPICS[lean] else 0.1) for t in TOPICS}
        if i % 11 != 10:  # every 11th Reel has no assignment: the fingerprint skips it
            assign(db, code, probs)
        ids.append(
            watch(
                db,
                actor.user_id,
                session,
                code,
                T0 + timedelta(hours=7 * i),
                dwell_ms=None if i % 7 == 6 else (800 if i % 5 == 0 else 4_000 + 1_000 * i),
                duration_ms=None if i % 4 == 0 else 15_000,
                caption=None if i % 9 == 0 else f"caption {i}",
            )
        )
    rebuild(db, actor.user_id)
    return ids


# ------------------------------------------------------------------ authorisation


def test_login_is_required(client: TestClient, alice: Actor) -> None:
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.get(f"{API}/topics/1/evidence?kind=exposure").status_code == 401
    # An extension token is not a dashboard login.
    assert (
        anonymous.get(f"{API}/topics/1/evidence?kind=exposure", headers=alice.bearer).status_code
        == 401
    )


def test_the_user_comes_from_the_cookie_never_from_the_request(
    client: TestClient, db: Session, alice: Actor, bob: Actor
) -> None:
    populate(db, alice)
    seed_topics(db)
    for extra in (f"&user_id={alice.user_id}", f"&owner={alice.user_id}"):
        response = bob.dashboard.get(f"{API}/topics/1/evidence?kind=exposure{extra}")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == [] and body["total"] == 0  # Bob has nothing, whatever he asks for


def test_another_users_reels_never_appear(db: Session, alice: Actor, bob: Actor) -> None:
    populate(db, alice)
    seed_topics(db)
    # Bob watched one Reel, of a shortcode Alice also watched: assignments are shared by Reel,
    # exposures are not.
    bob_session = new_session(db, bob.user_id, T0)
    bob_event = watch(
        db, bob.user_id, bob_session, "reel000", T0 + timedelta(hours=1), caption="BOB-ONLY"
    )
    rebuild(db, bob.user_id)

    mine = evidence(bob, 1, limit=100)
    assert [i["event_id"] for i in mine["items"]] == [str(bob_event)]
    assert mine["items"][0]["caption"] == "BOB-ONLY"

    theirs = evidence(alice, 1, limit=100)
    assert str(bob_event) not in {i["event_id"] for i in theirs["items"]}
    assert "BOB-ONLY" not in str(theirs)


def test_a_user_with_no_fingerprint_gets_an_empty_answer_not_someone_elses(
    db: Session, alice: Actor, bob: Actor
) -> None:
    populate(db, alice)
    body = evidence(bob, 1)
    assert body["evidence_available"] is False and body["reason"] == "no_fingerprint"
    assert body["items"] == [] and body["fingerprint"] is None and body["total"] == 0


def test_the_response_never_contains_another_users_ids(
    db: Session, alice: Actor, bob: Actor
) -> None:
    ids_a = populate(db, alice, reels=12)
    bob_session = new_session(db, bob.user_id, T0)
    bob_events = [
        watch(
            db, bob.user_id, bob_session, f"reel{i:03d}", T0 + timedelta(hours=i), caption=f"b{i}"
        )
        for i in range(5)
    ]
    rebuild(db, bob.user_id)
    text_a = str(evidence(alice, 1, limit=100)) + str(evidence(alice, 2, "engagement", limit=100))
    assert not any(str(e) in text_a for e in bob_events)
    text_b = str(evidence(bob, 1, limit=100))
    assert not any(str(e) in text_b for e in ids_a)
    assert bob.user_id not in text_a and alice.user_id not in text_b


def test_a_topic_that_does_not_exist_is_404_and_bad_parameters_are_422(
    db: Session, alice: Actor
) -> None:
    populate(db, alice, reels=5)
    get = lambda q: alice.dashboard.get(f"{API}/topics/{q}")  # noqa: E731
    assert get("999/evidence?kind=exposure").status_code == 404
    for bad in (
        "1/evidence",
        "1/evidence?kind=mood",
        "1/evidence?kind=exposure&limit=0",
        "1/evidence?kind=exposure&limit=101",
        "1/evidence?kind=exposure&offset=-1",
        "1/evidence?kind=exposure&limit=x",
        "abc/evidence?kind=exposure",
        "-1/evidence?kind=exposure",
        "40000/evidence?kind=exposure",
    ):
        assert get(bad).status_code == 422, bad


def test_the_endpoint_only_reads(client: TestClient, db: Session, alice: Actor) -> None:
    populate(db, alice, reels=5)
    assert (
        alice.dashboard.post(f"{API}/topics/1/evidence?kind=exposure", headers=CSRF).status_code
        == 405
    )
    assert (
        alice.dashboard.delete(f"{API}/topics/1/evidence?kind=exposure", headers=CSRF).status_code
        == 405
    )


# ------------------------------------------------------------------ the numbers add up


@pytest.mark.parametrize("kind", ["exposure", "engagement"])
@pytest.mark.parametrize("topic", [1, 2, 3])
def test_contributions_add_up_to_the_stored_topic_weight(
    db: Session, alice: Actor, kind: str, topic: int
) -> None:
    populate(db, alice)
    body = evidence(alice, topic, kind, limit=100)
    weight = snapshot_weight(db, alice.user_id, kind, topic)
    assert body["fingerprint"]["topic_weight"] == pytest.approx(weight, rel=1e-12)
    assert body["explanation"]["contributions_sum"] == pytest.approx(weight, rel=1e-9)
    assert body["explanation"]["coverage"] == pytest.approx(1.0, abs=1e-9)
    assert sum(i["contribution"] for i in body["items"]) == pytest.approx(weight, rel=1e-9)
    assert sum(i["share_of_topic"] for i in body["items"]) == pytest.approx(1.0, abs=1e-9)


def test_each_items_numbers_are_the_engines_own(db: Session, alice: Actor) -> None:
    populate(db, alice)
    body = evidence(alice, 2, "engagement", limit=100)
    last = datetime.fromisoformat(snapshot_last_observed(db, alice.user_id, "engagement"))
    for item in body["items"]:
        gain, decay, contribution = python_contribution(
            "engagement",
            CFG,
            dwell_ms=item["dwell_ms"],
            video_duration_ms=item["video_duration_ms"],
            probability=item["topic_score"],
            seconds_before_last=(
                last - datetime.fromisoformat(item["observed_at"])
            ).total_seconds(),
        )
        assert item["gain"] == pytest.approx(gain, rel=1e-12)
        assert item["recency"] == pytest.approx(decay, rel=1e-12)
        assert item["contribution"] == pytest.approx(contribution, rel=1e-12)
        assert item["contribution"] == pytest.approx(
            item["topic_score"] * item["gain"] * item["recency"], rel=1e-12
        )
        assert item["gain"] == engagement_gain(item["dwell_ms"], item["video_duration_ms"], CFG)


def snapshot_last_observed(db: Session, user_id: str, kind: str) -> str:
    db.expire_all()
    snap = (
        db.query(FingerprintSnapshot)
        .filter(FingerprintSnapshot.user_id == uuid.UUID(user_id), FingerprintSnapshot.kind == kind)
        .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
        .first()
    )
    assert snap is not None
    return str(snap.weights["last_observed_at"])


def test_exposure_counts_every_reel_once_engagement_counts_by_dwell(
    db: Session, alice: Actor
) -> None:
    populate(db, alice)
    exposure = evidence(alice, 1, "exposure", limit=100)["items"]
    engagement = evidence(alice, 1, "engagement", limit=100)["items"]
    assert {i["gain"] for i in exposure} == {1.0}
    assert min(i["gain"] for i in engagement) == 0.1  # skips count for little
    assert max(i["gain"] for i in engagement) > 1.0  # long views for more
    # A Reel with no dwell has no engagement gain, so it is absent there but present in exposure.
    no_dwell = {i["event_id"] for i in exposure if i["dwell_ms"] is None}
    assert no_dwell and no_dwell.isdisjoint({i["event_id"] for i in engagement})


def test_reels_the_fingerprint_skipped_are_not_in_the_evidence(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "known", {1: 0.7, 2: 0.2, 3: 0.1})
    kept = watch(db, alice.user_id, session, "known", T0)
    skipped = watch(db, alice.user_id, session, "unassigned", T0 + timedelta(hours=1))
    rebuild(db, alice.user_id)
    ids = {i["event_id"] for i in evidence(alice, 1)["items"]}
    assert str(kept) in ids and str(skipped) not in ids


def test_a_recent_reel_counts_for_more_than_an_old_one_with_the_same_score(
    db: Session, alice: Actor
) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "same", {1: 0.5, 2: 0.3, 3: 0.2})
    old = watch(db, alice.user_id, session, "same", T0)
    new = watch(db, alice.user_id, session, "same", T0 + timedelta(days=7))
    rebuild(db, alice.user_id)
    items = evidence(alice, 1)["items"]
    assert [i["event_id"] for i in items] == [str(new), str(old)]
    assert items[0]["recency"] == 1.0
    assert items[1]["recency"] == pytest.approx(0.5)  # one half-life of 7 days
    assert items[0]["contribution"] == pytest.approx(2 * items[1]["contribution"])


def test_the_explanation_says_how_reels_count_and_the_share_matches_the_dashboard(
    db: Session, alice: Actor
) -> None:
    populate(db, alice)
    for kind in ("exposure", "engagement"):
        body = evidence(alice, 1, kind)
        current = alice.dashboard.get(f"{API}/fingerprint/current?kind={kind}").json()
        shown = next(t["share"] for t in current["topics"] if t["topic_id"] == 1)
        assert body["fingerprint"]["topic_share"] == pytest.approx(shown, abs=1e-12)
        assert body["fingerprint"]["half_life_days"] == 7.0
        assert body["fingerprint"]["topic_weight"] / body["fingerprint"][
            "total_weight"
        ] == pytest.approx(shown)
        assert ("dwell" in body["explanation"]["gain_rule"]) == (kind == "engagement")
        assert "x gain x recency" in body["explanation"]["formula"]


def test_the_evidence_uses_the_settings_the_snapshot_was_built_with(
    db: Session, alice: Actor
) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    watch(db, alice.user_id, session, "r", T0)
    watch(db, alice.user_id, session, "r", T0 + timedelta(days=2))
    rebuild_user(
        db,
        uuid.UUID(alice.user_id),
        assignment_version=AV,
        topic_ids=TOPICS,
        config=FingerprintConfig(half_life_days=2.0),
    )
    db.commit()
    body = evidence(alice, 1)
    assert body["fingerprint"]["half_life_days"] == 2.0
    assert body["items"][1]["recency"] == pytest.approx(0.5)  # 2 days at half-life 2


def test_assignments_of_another_pipeline_version_are_not_used(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    assign(db, "r", {1: 0.99, 2: 0.005, 3: 0.005}, version="pv-other")
    watch(db, alice.user_id, session, "r", T0)
    rebuild(db, alice.user_id)
    (item,) = evidence(alice, 1)["items"]
    assert item["topic_score"] == 0.5


def test_reels_after_the_snapshot_are_not_in_its_evidence(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    included = watch(db, alice.user_id, session, "r", T0)
    rebuild(db, alice.user_id)
    later = watch(db, alice.user_id, session, "r", T0 + timedelta(days=1))  # not rebuilt yet
    ids = {i["event_id"] for i in evidence(alice, 1)["items"]}
    assert ids == {str(included)} and str(later) not in ids
    weight = snapshot_weight(db, alice.user_id, "exposure", 1)
    assert evidence(alice, 1)["explanation"]["contributions_sum"] == pytest.approx(weight)


def test_the_other_topic_has_evidence_too(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    for tid in (*TOPICS, OTHER_TOPIC_ID):  # a Reel assigned to `other`
        db.add(
            TopicAssignment(
                user_id=uuid.UUID(alice.user_id),
                reel_shortcode="odd",
                pipeline_version=AV,
                topic_id=tid,
                p_fused=1.0 if tid == OTHER_TOPIC_ID else 0.0,
            )
        )
    db.commit()
    event_id = watch(db, alice.user_id, session, "odd", T0)
    rebuild(db, alice.user_id)
    body = evidence(alice, OTHER_TOPIC_ID)
    assert [i["event_id"] for i in body["items"]] == [str(event_id)]
    assert body["items"][0]["topic_score"] == 1.0 and body["topic"]["slug"] == "other"
    # No Reel put weight on a topic it scored 0 for.
    assert evidence(alice, 1)["reason"] == "topic_not_in_fingerprint"


# ------------------------------------------------------------------ what each item shows


def test_each_item_has_the_link_caption_time_dwell_and_scores(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "Cabc123", {1: 0.6, 2: 0.3, 3: 0.1})
    event_id = watch(
        db,
        alice.user_id,
        session,
        "Cabc123",
        T0 + timedelta(minutes=5),
        dwell_ms=12_000,
        duration_ms=15_000,
        caption="Kohli six! 🏏",
    )
    rebuild(db, alice.user_id)
    (item,) = evidence(alice, 1, "engagement")["items"]
    assert item["event_id"] == str(event_id) and item["session_id"] == str(session)
    assert item["reel_url"] == "https://www.instagram.com/reel/Cabc123/"
    assert item["reel_shortcode"] == "Cabc123"
    assert item["caption"] == "Kohli six! 🏏" and item["caption_truncated"] is False
    assert datetime.fromisoformat(item["observed_at"]) == T0 + timedelta(minutes=5)
    assert (item["dwell_ms"], item["video_duration_ms"]) == (12_000, 15_000)
    assert item["topic_score"] == 0.6 and item["p_text"] == 0.6 and item["p_visual"] is None
    assert item["gain"] == pytest.approx(0.8)  # 12 s of 15 s
    assert item["recency"] == 1.0
    assert item["contribution"] == pytest.approx(0.6 * 0.8)
    assert item["share_of_topic"] == pytest.approx(1.0)


def test_a_missing_caption_stays_missing_and_a_long_one_is_cut_and_marked(
    db: Session, alice: Actor
) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    none = watch(db, alice.user_id, session, "r", T0, caption=None)
    long = watch(db, alice.user_id, session, "r", T0 + timedelta(hours=1), caption="x" * 1000)
    rebuild(db, alice.user_id)
    items = {i["event_id"]: i for i in evidence(alice, 1)["items"]}
    assert items[str(none)]["caption"] is None and items[str(none)]["caption_truncated"] is False
    assert len(items[str(long)]["caption"]) == CAPTION_MAX_CHARS
    assert items[str(long)]["caption_truncated"] is True


def test_a_reel_with_unknown_dwell_or_length_is_shown_as_unknown_not_zero(
    db: Session, alice: Actor
) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    watch(db, alice.user_id, session, "r", T0, dwell_ms=None, duration_ms=None)
    rebuild(db, alice.user_id)
    (item,) = evidence(alice, 1, "exposure")["items"]
    assert item["dwell_ms"] is None and item["video_duration_ms"] is None


# ------------------------------------------------------------------ pagination


def test_pages_are_ordered_by_contribution_and_cover_every_reel_exactly_once(
    db: Session, alice: Actor
) -> None:
    populate(db, alice, reels=45)
    first = evidence(alice, 1, limit=100)
    everything = [i["event_id"] for i in first["items"]]
    assert first["total"] == len(everything) and first["next_offset"] is None
    values = [i["contribution"] for i in first["items"]]
    assert values == sorted(values, reverse=True)

    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    while offset is not None:
        page = evidence(alice, 1, limit=7, offset=offset)
        assert page["total"] == first["total"] and page["offset"] == offset and page["limit"] == 7
        assert len(page["items"]) <= 7
        seen += [i["event_id"] for i in page["items"]]
        offset = page["next_offset"]
        pages += 1
    assert seen == everything  # same order, nothing repeated, nothing missing
    assert pages == math.ceil(first["total"] / 7)


def test_the_page_edges(db: Session, alice: Actor) -> None:
    populate(db, alice, reels=10)
    total = evidence(alice, 1)["total"]
    last = evidence(alice, 1, limit=3, offset=total - 1)
    assert len(last["items"]) == 1 and last["next_offset"] is None
    beyond = evidence(alice, 1, limit=3, offset=total + 50)
    assert beyond["items"] == [] and beyond["next_offset"] is None and beyond["total"] == total
    exact = evidence(alice, 1, limit=total)
    assert exact["next_offset"] is None and len(exact["items"]) == total
    assert evidence(alice, 1, limit=total - 1)["next_offset"] == total - 1


def test_equal_contributions_keep_a_stable_order_across_pages(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 0.5, 2: 0.3, 3: 0.2})
    for _ in range(9):  # nine identical Reels at one instant
        watch(db, alice.user_id, session, "r", T0)
    rebuild(db, alice.user_id)
    pages = [evidence(alice, 1, limit=2, offset=o)["items"] for o in (0, 2, 4, 6, 8)]
    flat = [i["event_id"] for page in pages for i in page]
    assert len(flat) == 9 == len(set(flat))
    assert flat == [i["event_id"] for i in evidence(alice, 1, limit=100)["items"]]


def test_the_default_page_size_is_twenty(db: Session, alice: Actor) -> None:
    populate(db, alice, reels=30)
    body = evidence(alice, 1)
    assert body["limit"] == 20 and len(body["items"]) == 20 and body["next_offset"] == 20


# ------------------------------------------------------------------ empty and unexplained cases


def test_a_topic_with_no_weight_gets_an_explanation_and_no_items(db: Session, alice: Actor) -> None:
    seed_topics(db)
    session = new_session(db, alice.user_id, T0)
    assign(db, "r", {1: 1.0, 2: 0.0, 3: 0.0})
    watch(db, alice.user_id, session, "r", T0)
    rebuild(db, alice.user_id)
    body = evidence(alice, 2)
    assert body["evidence_available"] is False and body["reason"] == "topic_not_in_fingerprint"
    assert body["items"] == [] and body["fingerprint"]["topic_weight"] == 0.0


def test_a_snapshot_without_provenance_is_not_explained_with_guesses(
    db: Session, alice: Actor
) -> None:
    populate(db, alice, reels=6)
    db.execute(text("UPDATE fingerprint_snapshots SET weights = weights - 'provenance'"))
    db.commit()
    body = evidence(alice, 1)
    assert body["evidence_available"] is False and body["reason"] == "snapshot_without_provenance"
    assert body["items"] == []


def test_the_newest_snapshot_is_the_one_explained(db: Session, alice: Actor) -> None:
    populate(db, alice, reels=6)
    before = evidence(alice, 1)["fingerprint"]["n_reels"]
    session = new_session(db, alice.user_id, T0 + timedelta(days=30))
    assign(db, "late", {1: 0.9, 2: 0.05, 3: 0.05})
    watch(db, alice.user_id, session, "late", T0 + timedelta(days=30))
    rebuild(db, alice.user_id)
    after = evidence(alice, 1)
    assert after["fingerprint"]["n_reels"] == before + 1
    assert after["items"][0]["reel_shortcode"] == "late"


# ------------------------------------------------------------------ cost


def test_the_number_of_queries_does_not_depend_on_the_page_or_the_data(
    db: Session, alice: Actor, migrated_engine: Engine
) -> None:
    populate(db, alice, reels=40)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        evidence(alice, 1, limit=5)
        small = len(statements)
        statements.clear()
        evidence(alice, 1, limit=100, offset=3)
        large = len(statements)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)
    assert small == large <= 8  # login, topic, snapshot, totals, page
    assert all("fingerprint_snapshots" not in s or "LIMIT" in s.upper() for s in statements)


def test_evidence_does_not_change_any_data(db: Session, alice: Actor) -> None:
    populate(db, alice, reels=8)
    counts = lambda: (  # noqa: E731
        db.query(ReelExposure).count(),
        db.query(FingerprintSnapshot).count(),
        db.query(TopicAssignment).count(),
        db.query(User).count(),
    )
    before = counts()
    for kind in ("exposure", "engagement"):
        evidence(alice, 1, kind)
    assert counts() == before

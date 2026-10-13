"""Security audit H-1: rate limiting and throttling (spec N6, ADR-005).

The first half is pure (the limiter, client address, the password cap). The second half drives the
real application against PostgreSQL with a fake clock, using the production defaults where they
matter (ADR-005's 60 a minute with a burst of 20) and strict custom limits where a test needs to
reach a limit quickly.
"""

import threading
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request
from support import CSRF, Actor, make_actor, register

from app.config import Settings
from app.ratelimit import Limits, RateLimiter, client_ip, password_work, request_class

PASSWORD = "correct horse battery staple"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def settings(**overrides: Any) -> Settings:
    return Settings(database_url="postgresql+psycopg://u:p@localhost/x", **overrides)


def fake_request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)}
    )


# ------------------------------------------------------------------ the limiter


def test_a_bucket_allows_its_burst_then_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(burst=3, per_minute=60, clock=clock)  # one token a second
    assert [limiter.check("k") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.check("k")
    assert wait == pytest.approx(1.0)
    clock.advance(0.4)
    assert limiter.check("k") == pytest.approx(0.6)  # asking again does not make it worse
    clock.advance(0.6)
    assert limiter.check("k") == 0.0


def test_a_bucket_refills_but_never_beyond_its_burst() -> None:
    clock = Clock()
    limiter = RateLimiter(burst=2, per_minute=120, clock=clock)
    limiter.check("k")
    limiter.check("k")
    clock.advance(3600)  # an hour later it is full, not overflowing
    assert [limiter.check("k") for _ in range(3)] == [0.0, 0.0, pytest.approx(0.5)]


def test_keys_are_independent() -> None:
    limiter = RateLimiter(burst=1, per_minute=1, clock=Clock())
    assert limiter.check("a") == 0.0 and limiter.check("a") > 0.0
    assert limiter.check("b") == 0.0


def test_the_table_is_bounded_so_random_keys_cannot_grow_it() -> None:
    limiter = RateLimiter(burst=1, per_minute=1, clock=Clock(), max_keys=100)
    for i in range(10_000):
        limiter.check(f"key-{i}")
    assert len(limiter) == 100


def test_peek_takes_nothing_and_spend_always_takes() -> None:
    clock = Clock()
    limiter = RateLimiter(burst=2, per_minute=60, clock=clock)
    assert limiter.peek("k") == 0.0 and limiter.peek("k") == 0.0
    limiter.spend("k")
    limiter.spend("k")
    limiter.spend("k")  # below zero is clamped: it cannot build up a debt
    assert limiter.peek("k") == pytest.approx(1.0)
    limiter.reset_key("k")
    assert limiter.peek("k") == 0.0


def test_a_limiter_needs_sane_parameters() -> None:
    for burst, rate in ((0, 1), (1, 0), (1, -5)):
        with pytest.raises(ValueError, match="burst"):
            RateLimiter(burst=burst, per_minute=rate)


def test_concurrent_callers_never_get_more_than_the_burst() -> None:
    limiter = RateLimiter(burst=20, per_minute=1, clock=Clock())
    allowed: list[float] = []

    def hit() -> None:
        allowed.append(limiter.check("shared"))

    threads = [threading.Thread(target=hit) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for wait in allowed if wait == 0.0) == 20


def test_the_default_ingest_limit_is_the_one_adr_005_accepted() -> None:
    limits = Limits.from_settings(settings())
    assert limits.ingest.burst == 20
    assert limits.ingest.rate * 60 == pytest.approx(60.0)


# ------------------------------------------------------------------ the client address


def test_an_unconfigured_server_trusts_nobody_s_forwarded_header() -> None:
    limits = Limits.from_settings(settings())
    assert client_ip(fake_request("203.0.113.9", "1.2.3.4"), limits) == "203.0.113.9"


def test_a_trusted_proxy_vouches_for_the_right_most_untrusted_entry() -> None:
    limits = Limits.from_settings(settings(trusted_proxies="10.0.0.0/8"))
    # the client sent "6.6.6.6" itself; the proxy appended the address it actually saw
    request = fake_request("10.0.0.2", "6.6.6.6, 198.51.100.7")
    assert client_ip(request, limits) == "198.51.100.7"
    chain = fake_request("10.0.0.2", "198.51.100.7, 10.1.1.1")  # a second proxy in the chain
    assert client_ip(chain, limits) == "198.51.100.7"


def test_a_peer_outside_the_trusted_network_cannot_use_the_header() -> None:
    limits = Limits.from_settings(settings(trusted_proxies="10.0.0.0/8"))
    assert client_ip(fake_request("198.51.100.20", "10.9.9.9, 1.1.1.1"), limits) == "198.51.100.20"


def test_a_malformed_chain_is_not_believed_and_ipv6_works() -> None:
    limits = Limits.from_settings(settings(trusted_proxies="10.0.0.0/8, fd00::/8"))
    assert client_ip(fake_request("10.0.0.2", "not-an-ip"), limits) == "10.0.0.2"
    assert client_ip(fake_request("fd00::2", "2001:db8::5"), limits) == "2001:db8::5"
    assert client_ip(fake_request("10.0.0.2"), limits) == "10.0.0.2"  # no header at all


# ------------------------------------------------------------------ password work


def test_only_so_many_password_hashes_run_at_once_and_the_rest_are_refused() -> None:
    limits = Limits.from_settings(settings(password_concurrency=2))
    request = fake_request("127.0.0.1")
    request.scope["app"] = type("A", (), {"state": type("S", (), {"limits": limits})()})()
    with password_work(request), password_work(request):
        with pytest.raises(HTTPException) as busy, password_work(request):
            pass
        assert busy.value.status_code == 503 and busy.value.detail == "busy"
        assert busy.value.headers == {"Retry-After": "1"}
    with password_work(request):  # a slot is free again
        pass


def test_request_classes() -> None:
    def cls(method: str, path: str, via: str = "cookie", stray_auth: bool = False) -> str:
        headers = [(b"authorization", b"Bearer x")] if stray_auth else []
        scope = {"type": "http", "method": method, "path": path, "headers": headers}
        return request_class(Request(scope), via)

    assert cls("POST", "/api/v1/events", "token") == "ingest:events"
    assert cls("POST", "/api/v1/events/abc/frames", "token") == "ingest:frames"
    assert cls("DELETE", "/api/v1/me/data") == "destructive"
    assert cls("DELETE", "/api/v1/consent", "token") == "destructive"
    assert cls("PUT", "/api/v1/consent", "token") == "token"
    assert cls("GET", "/api/v1/fingerprint/current") == "dashboard"
    # the class follows the credential that was accepted, not a header a caller added
    assert cls("GET", "/api/v1/sessions", "cookie", stray_auth=True) == "dashboard"
    assert cls("PUT", "/api/v1/consent", "token", stray_auth=False) == "token"


# ------------------------------------------------------------------ the application (PostgreSQL)

db_only = pytest.mark.db


def install(client: TestClient, clock: Clock | None = None, **overrides: Any) -> Clock:
    clock = clock or Clock()
    client.app.state.limits = Limits.from_settings(settings(**overrides), clock=clock)  # type: ignore[attr-defined]
    return clock


def login(client: TestClient, email: str, password: str = PASSWORD, ip: str | None = None) -> Any:
    headers = dict(CSRF)
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=headers
    )


@db_only
def test_failed_logins_for_one_email_lock_it_before_any_password_work(
    client: TestClient, db: Session
) -> None:
    clock = install(client)
    register(client, db, "victim@example.test")
    for _ in range(5):
        assert login(client, "victim@example.test", "wrong password").status_code == 401
    locked = login(client, "victim@example.test", PASSWORD)  # even the right password
    assert locked.status_code == 429 and locked.json() == {"detail": "rate_limited"}
    assert 1 <= int(locked.headers["Retry-After"]) <= 181  # up to 3 minutes, rounded up
    clock.advance(180)  # one attempt is allowed again
    assert login(client, "victim@example.test", PASSWORD).status_code == 200


@db_only
def test_the_lock_is_per_email_and_a_success_clears_the_count(
    client: TestClient, db: Session
) -> None:
    install(client, auth_ip_burst=100, auth_ip_per_minute=600)  # only the email lock is tested here
    register(client, db, "a@example.test")
    register(client, db, "b@example.test")
    for _ in range(4):
        login(client, "a@example.test", "wrong password")
    assert login(client, "b@example.test").status_code == 200  # another account is untouched
    assert login(client, "a@example.test").status_code == 200  # a success resets a's failures
    for _ in range(4):
        login(client, "a@example.test", "wrong password")
    assert login(client, "a@example.test").status_code == 200  # it would be 429 without the reset


@db_only
def test_an_unknown_email_is_locked_exactly_like_a_known_one(client: TestClient) -> None:
    """No oracle: the response sequence does not say whether the account exists."""
    install(client)
    codes = [
        login(client, "nobody@example.test", "whatever-long-enough").status_code for _ in range(7)
    ]
    assert codes == [401, 401, 401, 401, 401, 429, 429]


@db_only
def test_one_address_cannot_try_many_emails_faster_than_the_address_limit(
    client: TestClient,
) -> None:
    install(client)  # auth: burst 10 per address
    codes = [
        login(client, f"user{i}@example.test", "whatever-long-enough").status_code
        for i in range(14)
    ]
    assert codes[:10] == [401] * 10 and set(codes[10:]) == {429}


@db_only
def test_registration_is_throttled_by_address_too(client: TestClient) -> None:
    install(client)
    body = {"email": "x@example.test", "password": PASSWORD, "invite_code": "nope"}
    codes = [
        client.post("/api/v1/auth/register", json=body, headers=CSRF).status_code for _ in range(12)
    ]
    assert codes[:10] == [403] * 10 and set(codes[10:]) == {429}


@db_only
def test_a_busy_password_pool_is_a_503_with_retry_after(client: TestClient, db: Session) -> None:
    install(client, password_concurrency=1)
    register(client, db, "p@example.test")
    slots = client.app.state.limits.password_slots  # type: ignore[attr-defined]
    assert slots.acquire(blocking=False)  # another login is in the middle of hashing
    try:
        busy = login(client, "p@example.test")
        assert busy.status_code == 503 and busy.json() == {"detail": "busy"}
        assert busy.headers["Retry-After"] == "1"
    finally:
        slots.release()
    assert login(client, "p@example.test").status_code == 200


@db_only
def test_an_unauthenticated_flood_is_counted_by_address(client: TestClient) -> None:
    install(client, api_ip_burst=30, api_ip_per_minute=60)
    codes = [client.get("/api/v1/fingerprint/current?kind=exposure").status_code for _ in range(40)]
    assert codes[:30] == [401] * 30 and set(codes[30:]) == {429}
    assert client.get("/healthz").status_code == 200  # liveness is not an API request


@db_only
def test_a_spoofed_forwarded_header_does_not_buy_a_new_bucket(client: TestClient) -> None:
    install(client, api_ip_burst=5, api_ip_per_minute=60)
    codes = [
        client.get("/api/v1/sessions", headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
        for i in range(8)
    ]
    assert codes[:5] == [401] * 5 and set(codes[5:]) == {429}


@db_only
def test_behind_a_configured_proxy_each_real_client_has_its_own_bucket(
    client: TestClient,
) -> None:
    install(client, api_ip_burst=3, api_ip_per_minute=60, trusted_proxies="10.0.0.0/8")
    proxy = TestClient(client.app, base_url="http://localhost", client=("10.0.0.2", 5000))

    def hit(real: str, sent: str) -> int:
        return proxy.get(
            "/api/v1/sessions", headers={"X-Forwarded-For": f"{sent}, {real}"}
        ).status_code

    # The client prepends a different forged address each time; the proxy appended the real one.
    assert [hit("198.51.100.1", f"6.6.6.{i}") for i in range(4)] == [401, 401, 401, 429]
    assert hit("198.51.100.2", "6.6.6.0") == 401  # a different real client has its own bucket
    # A caller that is not the proxy cannot use the header at all.
    stranger = TestClient(client.app, base_url="http://localhost", client=("198.51.100.99", 5000))
    forged = [
        stranger.get("/api/v1/sessions", headers={"X-Forwarded-For": f"7.7.7.{i}"}).status_code
        for i in range(4)
    ]
    assert forged == [401, 401, 401, 429]


# ---- ingest (ADR-005: 60 a minute, burst 20)


def grant(client: TestClient, actor: Actor) -> None:
    assert (
        client.put(
            "/api/v1/consent", json={"policy_version": "p1"}, headers=actor.bearer
        ).status_code
        == 200
    )


@db_only
def test_ingest_stops_after_the_burst_and_resumes_as_the_bucket_refills(
    client: TestClient, db: Session
) -> None:
    clock = install(client)  # the production defaults: burst 20, 60 a minute
    actor = make_actor(client, db, "ing@example.test")
    grant(client, actor)  # a token request, not ingest
    batch: dict[str, list[Any]] = {
        "events": []
    }  # invalid, but it is counted before it is parsed: 422 each time

    def post() -> Any:
        return client.post("/api/v1/events", json=batch, headers=actor.bearer)

    codes = [post().status_code for _ in range(23)]
    assert codes[:20] == [422] * 20 and set(codes[20:]) == {429}
    refused = post()
    assert refused.json() == {"detail": "rate_limited"}
    assert int(refused.headers["Retry-After"]) == 1
    clock.advance(1.0)  # 60 a minute is one a second
    assert post().status_code == 422
    assert post().status_code == 429


@db_only
def test_a_refused_ingest_stores_nothing(client: TestClient, db: Session) -> None:
    install(client, ingest_burst=1, ingest_per_minute=1)
    actor = make_actor(client, db, "none@example.test")
    grant(client, actor)
    sid = client.post("/api/v1/sessions", headers=actor.bearer).json()["id"]
    import uuid
    from datetime import UTC, datetime

    def event() -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "session_id": sid,
            "reel_shortcode": "Cabc",
            "observed_at": datetime.now(UTC).isoformat(),
            "dwell_ms": 1000,
            "video_duration_ms": 5000,
            "video_duration_status": "available",
            "creator_handle": {"value": None, "status": "unavailable"},
            "caption": {"value": None, "status": "unavailable"},
            "caption_truncated": False,
            "hashtags": {"value": None, "status": "unavailable"},
            "frame_count": 0,
            "adapter_version": "t",
        }

    assert (
        client.post("/api/v1/events", json={"events": [event()]}, headers=actor.bearer).status_code
        == 200
    )
    second = client.post("/api/v1/events", json={"events": [event()]}, headers=actor.bearer)
    assert second.status_code == 429
    from app.models import ReelExposure

    db.expire_all()
    assert db.query(ReelExposure).count() == 1


@db_only
def test_frames_and_events_have_separate_buckets(client: TestClient, db: Session) -> None:
    install(client, ingest_burst=2, ingest_per_minute=1)
    actor = make_actor(client, db, "sep@example.test")
    grant(client, actor)

    def ev() -> int:
        return client.post("/api/v1/events", json={"events": []}, headers=actor.bearer).status_code

    def fr() -> int:
        return client.post(
            "/api/v1/events/00000000-0000-4000-8000-000000000000/frames", headers=actor.bearer
        ).status_code

    assert [ev(), ev(), ev()] == [422, 422, 429]
    assert fr() != 429  # its own bucket is untouched


@db_only
def test_one_users_limit_does_not_slow_another_and_extra_tokens_do_not_reset_it(
    client: TestClient, db: Session
) -> None:
    install(client, ingest_burst=2, ingest_per_minute=1)
    a = make_actor(client, db, "a@example.test")
    b = make_actor(client, db, "b@example.test")
    for actor in (a, b):
        grant(client, actor)

    def post(actor: Actor) -> int:
        return client.post("/api/v1/events", json={"events": []}, headers=actor.bearer).status_code

    assert [post(a), post(a), post(a)] == [422, 422, 429]
    assert post(b) == 422  # not affected
    token2 = a.dashboard.post("/api/v1/extension-tokens", headers=CSRF).json()["token"]
    other_device = {"Authorization": f"Bearer {token2}"}
    assert (
        client.post("/api/v1/events", json={"events": []}, headers=other_device).status_code == 429
    )


@db_only
def test_other_extension_requests_and_dashboard_requests_have_their_own_limits(
    client: TestClient, db: Session
) -> None:
    actor = make_actor(client, db, "classes@example.test")  # its own requests are not counted below
    install(client, token_burst=3, token_per_minute=1, dashboard_burst=3, dashboard_per_minute=1)
    token_calls = [
        client.put(
            "/api/v1/consent", json={"policy_version": "p1"}, headers=actor.bearer
        ).status_code
        for _ in range(5)
    ]
    assert token_calls[:3] == [200] * 3 and set(token_calls[3:]) == {429}
    page = [actor.dashboard.get("/api/v1/sessions").status_code for _ in range(5)]
    assert page[:3] == [200] * 3 and set(page[3:]) == {429}


@db_only
def test_deletions_are_limited_harder(client: TestClient, db: Session) -> None:
    install(client, destructive_burst=2, destructive_per_minute=1)
    actor = make_actor(client, db, "del@example.test")
    codes = [
        actor.dashboard.delete(
            f"/api/v1/sessions/00000000-0000-4000-8000-00000000000{i}", headers=CSRF
        ).status_code
        for i in range(4)
    ]
    assert codes[:2] == [200, 200] and set(codes[2:]) == {429}


@db_only
def test_limits_reset_for_a_new_application_and_the_defaults_are_not_loosened(
    client: TestClient,
) -> None:
    limits = client.app.state.limits  # type: ignore[attr-defined]
    assert limits.ingest.burst == 20 and limits.ingest.rate * 60 == pytest.approx(60.0)
    assert limits.auth_ip.burst == 10 and limits.login_failures.burst == 5
    assert limits.password_slots._value == 4


@db_only
def test_a_429_carries_no_user_data_and_no_stack(client: TestClient, db: Session) -> None:
    install(client, ingest_burst=1, ingest_per_minute=1)
    actor = make_actor(client, db, "shape@example.test")
    grant(client, actor)
    client.post("/api/v1/events", json={"events": []}, headers=actor.bearer)
    refused = client.post("/api/v1/events", json={"events": []}, headers=actor.bearer)
    assert refused.status_code == 429 and refused.json() == {"detail": "rate_limited"}
    assert set(refused.headers) >= {"retry-after"}

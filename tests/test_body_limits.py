"""Security audit H-2: every route under /api/ has a body limit, and errors never echo input.

No database needed: a request that is over its limit is refused before any route runs.
"""

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.body_limit import BodyLimitMiddleware, BodyTooLarge
from app.main import DEFAULT_BODY_BYTES, app
from app.routers.events import HARD_BODY_CEILING
from app.routers.events import MAX_BODY_BYTES as EVENTS_LIMIT

CSRF = {"X-Requested-With": "ref-dashboard"}
BIG = b"x" * (DEFAULT_BODY_BYTES + 1)


@pytest.fixture
def web() -> TestClient:
    return TestClient(app, base_url="http://localhost")


# ------------------------------------------------------------------ the middleware on its own


class Recorder:
    """An ASGI app that reads the whole body and, like FastAPI, turns any failure into a 400."""

    def __init__(self, *, swallow: bool) -> None:
        self.swallow = swallow
        self.calls = 0
        self.bytes_read = 0

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        self.calls += 1
        try:
            while True:
                message = await receive()
                self.bytes_read += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
        except BodyTooLarge:
            if not self.swallow:
                raise
            body = b'{"detail":"error parsing the body"}'
            status = 400
        else:
            body, status = b"{}", 200
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": body})


def wrapped(app_: Recorder, **kw) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(
        BodyLimitMiddleware(app_, rules=[(r"/api/special", 100)], default_limit=10, **kw),
        base_url="http://localhost",
    )


def test_a_declared_length_over_the_limit_is_refused_without_calling_the_app() -> None:
    inner = Recorder(swallow=False)
    response = wrapped(inner).post("/api/other", content=b"x" * 11)
    assert response.status_code == 413 and response.json() == {"detail": "payload_too_large"}
    assert inner.calls == 0


def test_a_body_that_grows_past_the_limit_is_413_even_if_the_app_swallows_the_error() -> None:
    """FastAPI answers 400 when a body cannot be read; the limit must still win."""

    def chunks() -> Iterator[bytes]:
        for _ in range(1000):
            yield b"y" * 100

    inner = Recorder(swallow=True)
    response = wrapped(inner).post("/api/other", content=chunks())  # chunked: no Content-Length
    assert response.status_code == 413
    assert inner.bytes_read <= 10 + 100  # abandoned at the limit, at most one chunk past it


def test_a_body_that_grows_past_the_limit_is_413_when_the_error_propagates() -> None:
    inner = Recorder(swallow=False)
    response = wrapped(inner).post("/api/other", content=(b"z" * 50 for _ in range(100)))
    assert response.status_code == 413


def test_a_body_inside_the_limit_passes_and_a_specific_rule_beats_the_default() -> None:
    inner = Recorder(swallow=False)
    client = wrapped(inner)
    assert client.post("/api/other", content=b"x" * 10).status_code == 200
    assert client.post("/api/special", content=b"x" * 100).status_code == 200  # rule: 100
    assert client.post("/api/special", content=b"x" * 101).status_code == 413
    assert client.post("/api/other", content=b"x" * 11).status_code == 413  # default: 10


def test_paths_outside_api_are_not_limited() -> None:
    inner = Recorder(swallow=False)
    assert wrapped(inner).post("/healthz", content=b"x" * 5000).status_code == 200


def test_no_default_means_only_the_listed_paths_are_limited() -> None:
    inner = Recorder(swallow=False)
    client = TestClient(
        BodyLimitMiddleware(inner, rules=[(r"/api/special", 5)]), base_url="http://localhost"
    )
    assert client.post("/api/other", content=b"x" * 5000).status_code == 200
    assert client.post("/api/special", content=b"x" * 6).status_code == 413


# ------------------------------------------------------------------ the real application


def api_routes() -> list[tuple[str, str]]:
    """Every body-carrying route, read from the application's own OpenAPI schema."""
    found = []
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api/"):
            for method in operations:
                if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                    found.append((method.upper(), path))
    return sorted(found)


def concrete(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", "00000000-0000-4000-8000-000000000000", path)


def test_the_route_list_is_not_empty_and_covers_the_unauthenticated_ones() -> None:
    routes = api_routes()
    assert ("POST", "/api/v1/auth/login") in routes and ("POST", "/api/v1/auth/register") in routes
    assert len(routes) >= 10


@pytest.mark.parametrize(("method", "path"), api_routes())
def test_every_route_refuses_an_oversized_body_before_it_runs(
    web: TestClient, method: str, path: str
) -> None:
    """Discovered from the application, so a route added later is covered without editing this."""
    url = concrete(path)
    response = web.request(method, url, content=BIG, headers=CSRF)
    if url.endswith("/events") or url.endswith("/frames"):
        # These have their own, larger limits: 17 KB is allowed through to authentication.
        assert response.status_code != 413
    else:
        assert response.status_code == 413, (method, path, response.status_code)
        assert response.json() == {"detail": "payload_too_large"}


def test_events_allow_their_documented_larger_body_and_refuse_beyond_it(web: TestClient) -> None:
    mid = web.post("/api/v1/events", content=b"x" * (DEFAULT_BODY_BYTES + 1), headers=CSRF)
    assert mid.status_code == 401  # reached authentication
    # The exact limit is applied after authentication (401, 403, then 413); without credentials a
    # body just over it still gets 401. A grossly larger one is cut off by the middleware.
    over = web.post("/api/v1/events", content=b"x" * (EVENTS_LIMIT + 1), headers=CSRF)
    assert over.status_code == 401
    gross = web.post("/api/v1/events", content=b"x" * (HARD_BODY_CEILING + 1), headers=CSRF)
    assert gross.status_code == 413


def test_a_huge_unauthenticated_login_is_refused_not_parsed(web: TestClient) -> None:
    body = b'{"email":"a@b.co","password":"' + b"x" * 5_000_000 + b'"}'
    response = web.post(
        "/api/v1/auth/login", content=body, headers={**CSRF, "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert len(response.content) < 100  # and it does not repeat the request


def test_a_chunked_login_body_is_cut_off_too(web: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"email":"a@b.co","password":"'
        for _ in range(1000):
            yield b"x" * 1024
        yield b'"}'

    response = web.post(
        "/api/v1/auth/login", content=chunks(), headers={**CSRF, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_a_normal_login_body_is_still_accepted_by_the_parser(web: TestClient) -> None:
    response = web.post("/api/v1/auth/login", json={"email": "a@b.co"}, headers=CSRF)
    assert response.status_code == 422  # missing password: validation, not 413


# ------------------------------------------------------------------ validation errors never echo input


def test_a_validation_error_carries_locations_and_types_only(web: TestClient) -> None:
    response = web.post(
        "/api/v1/auth/register",
        json={"email": "a@b.co", "password": "short-pw", "invite_code": "INVITE-SECRET-123"},
        headers=CSRF,
    )
    assert response.status_code == 422
    text = response.text
    assert "short-pw" not in text and "INVITE-SECRET-123" not in text and "a@b.co" not in text
    detail = response.json()["detail"]
    assert detail == [{"loc": ["body", "password"], "type": "string_too_short"}]
    assert set(detail[0]) == {"loc", "type"}


def test_a_large_invalid_value_is_not_reflected(web: TestClient) -> None:
    long_password = "p" * 12_000  # under the 16 KB body limit, over the 256 character field limit
    response = web.post(
        "/api/v1/auth/login", json={"email": "a@b.co", "password": long_password}, headers=CSRF
    )
    assert response.status_code == 422
    assert "pppp" not in response.text and len(response.content) < 300


def test_the_error_body_is_strict_json(web: TestClient) -> None:
    response = web.post("/api/v1/auth/login", content=b"{not json", headers=CSRF)
    assert response.status_code == 422
    json.loads(response.text)
    assert "not json" not in response.text

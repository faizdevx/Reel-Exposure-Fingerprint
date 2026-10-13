"""Authentication and authorisation tests (spec §7, N6). Need a database (TEST_DATABASE_URL)."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from support import CSRF, PASSWORD, login, make_invite, register

from app.auth_deps import SESSION_COOKIE
from app.config import Settings, get_settings
from app.models import ExtensionToken, Invite, User, UserSession

pytestmark = pytest.mark.db

API = "/api/v1"


def set_cookie_header(response) -> str:  # type: ignore[no-untyped-def]
    header = response.headers.get("set-cookie")
    assert header, "no Set-Cookie header"
    return str(header)


def create_token(client: TestClient, label: str | None = None) -> dict[str, str]:
    response = client.post(f"{API}/extension-tokens", json={"label": label}, headers=CSRF)
    assert response.status_code == 201, response.text
    body: dict[str, str] = response.json()
    return body


@pytest.fixture
def alice(client: TestClient, db: Session) -> dict[str, str]:
    """Registered and logged in. The client now carries Alice's session cookie."""
    user = register(client, db, "alice@example.test")
    assert login(client, "alice@example.test").status_code == 200
    return {"id": user["id"], "email": "alice@example.test"}


# ------------------------------------------------------------------ register (invite-only)


def test_register_with_valid_invite(client: TestClient, db: Session) -> None:
    code = make_invite(db)
    response = client.post(
        f"{API}/auth/register",
        json={"email": "New@Example.test ", "password": PASSWORD, "invite_code": code},
        headers=CSRF,
    )
    assert response.status_code == 201
    assert response.json()["email"] == "new@example.test"  # trimmed and lower-cased
    invite = db.get(Invite, hashlib.sha256(code.encode()).digest())
    assert invite is not None and invite.used_at is not None
    assert str(invite.used_by) == response.json()["id"]


def test_register_stores_an_argon2id_hash_not_the_password(client: TestClient, db: Session) -> None:
    register(client, db, "a@example.test")
    stored = db.query(User).one().password_hash
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_register_requires_a_valid_invite(client: TestClient, db: Session) -> None:
    def attempt(code: str) -> int:
        return client.post(
            f"{API}/auth/register",
            json={"email": "x@example.test", "password": PASSWORD, "invite_code": code},
            headers=CSRF,
        ).status_code

    assert attempt("not-a-real-code") == 403
    assert attempt(make_invite(db, expires_in=timedelta(seconds=-5))) == 403  # expired
    assert db.query(User).count() == 0


def test_invite_is_single_use(client: TestClient, db: Session) -> None:
    code = make_invite(db)

    def attempt(email: str) -> int:
        return client.post(
            f"{API}/auth/register",
            json={"email": email, "password": PASSWORD, "invite_code": code},
            headers=CSRF,
        ).status_code

    assert attempt("first@example.test") == 201
    assert attempt("second@example.test") == 403
    assert db.query(User).count() == 1


def test_duplicate_email_conflicts_and_does_not_burn_the_invite(
    client: TestClient, db: Session
) -> None:
    register(client, db, "dup@example.test")
    code = make_invite(db)
    response = client.post(
        f"{API}/auth/register",
        json={"email": "dup@example.test", "password": PASSWORD, "invite_code": code},
        headers=CSRF,
    )
    assert response.status_code == 409
    invite = db.get(Invite, hashlib.sha256(code.encode()).digest())
    assert invite is not None and invite.used_at is None


def test_unknown_invite_cannot_probe_for_existing_emails(client: TestClient, db: Session) -> None:
    register(client, db, "taken@example.test")
    response = client.post(
        f"{API}/auth/register",
        json={"email": "taken@example.test", "password": PASSWORD, "invite_code": "nope"},
        headers=CSRF,
    )
    assert response.status_code == 403  # not 409: no invite, no information


def test_register_rejects_weak_password_and_bad_email(client: TestClient, db: Session) -> None:
    code = make_invite(db)
    short = client.post(
        f"{API}/auth/register",
        json={"email": "a@example.test", "password": "short", "invite_code": code},
        headers=CSRF,
    )
    bad_email = client.post(
        f"{API}/auth/register",
        json={"email": "not-an-email", "password": PASSWORD, "invite_code": code},
        headers=CSRF,
    )
    assert short.status_code == 422
    assert bad_email.status_code == 422


def test_register_rejects_user_id_in_body(client: TestClient, db: Session) -> None:
    response = client.post(
        f"{API}/auth/register",
        json={
            "email": "a@example.test",
            "password": PASSWORD,
            "invite_code": make_invite(db),
            "id": str(uuid.uuid4()),
        },
        headers=CSRF,
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ login


def test_valid_login(client: TestClient, db: Session) -> None:
    user = register(client, db, "alice@example.test")
    response = login(client, "ALICE@example.test")  # email is case-insensitive
    assert response.status_code == 200
    assert response.json() == {"id": user["id"], "email": "alice@example.test"}
    assert SESSION_COOKIE in response.cookies


def test_invalid_login_is_the_same_for_wrong_password_and_unknown_email(
    client: TestClient, db: Session
) -> None:
    register(client, db, "alice@example.test")
    wrong_password = login(client, "alice@example.test", "wrong password here")
    unknown_email = login(client, "nobody@example.test")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert SESSION_COOKIE not in wrong_password.cookies
    assert SESSION_COOKIE not in unknown_email.cookies
    assert db.query(UserSession).count() == 0


def test_login_requires_the_csrf_header(client: TestClient, db: Session) -> None:
    register(client, db, "alice@example.test")
    response = client.post(
        f"{API}/auth/login", json={"email": "alice@example.test", "password": PASSWORD}
    )
    assert response.status_code == 403
    assert db.query(UserSession).count() == 0


def test_login_rejects_user_id_in_body(client: TestClient, db: Session) -> None:
    user = register(client, db, "alice@example.test")
    response = client.post(
        f"{API}/auth/login",
        json={"email": "alice@example.test", "password": PASSWORD, "user_id": user["id"]},
        headers=CSRF,
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ session cookie


def test_session_cookie_attributes(client: TestClient, db: Session) -> None:
    register(client, db, "alice@example.test")
    header = set_cookie_header(login(client, "alice@example.test")).lower()
    assert f"{SESSION_COOKIE}=" in header
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    assert f"max-age={168 * 3600}" in header
    assert "secure" not in header.replace("samesite", "")  # local http: not Secure


def test_session_cookie_is_secure_in_production(client: TestClient, db: Session) -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://x:y@localhost/z",
    )
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]
    register(client, db, "alice@example.test")
    header = set_cookie_header(login(client, "alice@example.test")).lower()
    assert "secure" in header
    assert "httponly" in header and "samesite=lax" in header


def test_cookie_holds_a_random_id_and_only_its_hash_is_stored(
    client: TestClient, db: Session
) -> None:
    register(client, db, "alice@example.test")
    cookie = login(client, "alice@example.test").cookies[SESSION_COOKIE]
    assert len(cookie) == 43
    row = db.query(UserSession).one()
    assert row.id_sha256 == hashlib.sha256(cookie.encode()).digest()
    dump = db.execute(text("SELECT row_to_json(t)::text FROM user_sessions t")).scalar_one()
    assert cookie not in dump
    assert row.expires_at > datetime.now(UTC) + timedelta(hours=167)


def test_each_login_gets_a_new_session_id(client: TestClient, db: Session) -> None:
    register(client, db, "alice@example.test")
    first = login(client, "alice@example.test").cookies[SESSION_COOKIE]
    second = login(client, "alice@example.test").cookies[SESSION_COOKIE]
    assert first != second
    assert db.query(UserSession).count() == 2


def test_cookie_authenticates_and_identifies_the_user(
    client: TestClient, alice: dict[str, str]
) -> None:
    response = client.get("/_probe/cookie")
    assert response.status_code == 200
    assert response.json() == {"user_id": alice["id"], "via": "cookie"}


def test_forged_and_expired_cookies_are_rejected(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    real = client.cookies[SESSION_COOKIE]
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, "x" * 43)
    assert client.get("/_probe/cookie").status_code == 401

    client.cookies.set(SESSION_COOKIE, real)
    assert client.get("/_probe/cookie").status_code == 200
    db.execute(
        text(
            "UPDATE user_sessions SET expires_at = now() - interval '1 second', "
            "created_at = now() - interval '2 seconds'"
        )
    )
    db.commit()
    assert client.get("/_probe/cookie").status_code == 401


def test_state_changing_cookie_requests_need_the_csrf_header(
    client: TestClient, alice: dict[str, str]
) -> None:
    assert client.post("/_probe/cookie").status_code == 403
    assert (
        client.post("/_probe/cookie", headers={"X-Requested-With": "XMLHttpRequest"}).status_code
        == 403
    )
    assert client.post("/_probe/cookie", headers=CSRF).status_code == 200
    assert client.get("/_probe/cookie").status_code == 200  # safe methods are exempt


def test_unauthenticated_requests_get_401_before_csrf_is_considered(client: TestClient) -> None:
    assert client.post("/_probe/cookie").status_code == 401
    assert client.post("/_probe/cookie", headers=CSRF).status_code == 401


# ------------------------------------------------------------------ logout


def test_logout_ends_the_session(client: TestClient, db: Session, alice: dict[str, str]) -> None:
    cookie = client.cookies[SESSION_COOKIE]
    response = client.post(f"{API}/auth/logout", headers=CSRF)
    assert response.status_code == 204
    cleared = set_cookie_header(response).lower()
    assert f"{SESSION_COOKIE}=" in cleared and ("max-age=0" in cleared or "expires=" in cleared)
    assert db.query(UserSession).count() == 0
    # Replaying the old cookie must fail: the session is gone server-side, not just in the browser.
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/_probe/cookie").status_code == 401


def test_logout_only_ends_the_current_session(client: TestClient, db: Session) -> None:
    register(client, db, "alice@example.test")
    login(client, "alice@example.test")
    other_device = TestClient(client.app, base_url="http://localhost")
    login(other_device, "alice@example.test")
    assert db.query(UserSession).count() == 2
    assert client.post(f"{API}/auth/logout", headers=CSRF).status_code == 204
    assert other_device.get("/_probe/cookie").status_code == 200


def test_logout_needs_authentication_and_csrf(client: TestClient, alice: dict[str, str]) -> None:
    assert client.post(f"{API}/auth/logout").status_code == 403  # logged in, header missing
    client.cookies.clear()
    assert client.post(f"{API}/auth/logout", headers=CSRF).status_code == 401


# ------------------------------------------------------------------ missing authentication


@pytest.mark.parametrize("path", ["/_probe/cookie", "/_probe/token", "/_probe/any"])
def test_missing_authentication_is_401(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_bearer_challenge_header(client: TestClient) -> None:
    response = client.get("/_probe/token")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "path_and_method",
    [
        ("POST", f"{API}/extension-tokens"),
        ("DELETE", f"{API}/extension-tokens/{uuid.uuid4()}"),
        ("POST", f"{API}/auth/logout"),
    ],
)
def test_protected_endpoints_return_401_without_authentication(
    client: TestClient, path_and_method: tuple[str, str]
) -> None:
    method, path = path_and_method
    assert client.request(method, path, headers=CSRF).status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer ", "Bearer not-a-token", "Basic dXNlcjpwYXNz", "bearer " + "a" * 43],
)
def test_malformed_or_unknown_bearer_is_401(client: TestClient, header: str) -> None:
    assert client.get("/_probe/token", headers={"Authorization": header}).status_code == 401


# ------------------------------------------------------------------ extension tokens


def test_token_is_returned_once_and_only_its_hash_is_stored(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    created = create_token(client, "laptop")
    token = created["token"]
    assert len(token) == 43

    row = db.get(ExtensionToken, uuid.UUID(created["id"]))
    assert row is not None
    assert row.token_sha256 == hashlib.sha256(token.encode()).digest()
    assert row.token_sha256 != token.encode()
    assert row.label == "laptop"
    assert str(row.user_id) == alice["id"]
    # The raw token appears nowhere in the stored row.
    dump = db.execute(text("SELECT row_to_json(t)::text FROM extension_tokens t")).scalar_one()
    assert token not in dump


def test_token_creation_response_is_not_cacheable(
    client: TestClient, alice: dict[str, str]
) -> None:
    response = client.post(f"{API}/extension-tokens", json={}, headers=CSRF)
    assert response.headers["cache-control"] == "no-store"


def test_token_can_be_created_without_a_body(client: TestClient, alice: dict[str, str]) -> None:
    response = client.post(f"{API}/extension-tokens", headers=CSRF)
    assert response.status_code == 201
    assert response.json()["label"] is None


def test_token_is_never_shown_again(client: TestClient, alice: dict[str, str]) -> None:
    created = create_token(client)
    for path in (f"{API}/extension-tokens", f"{API}/extension-tokens/{created['id']}"):
        assert client.get(path).status_code in (404, 405)  # there is no read endpoint


def test_token_creation_needs_csrf_header(client: TestClient, alice: dict[str, str]) -> None:
    assert client.post(f"{API}/extension-tokens", json={}).status_code == 403


def test_token_creation_rejects_user_id_in_body(client: TestClient, alice: dict[str, str]) -> None:
    response = client.post(
        f"{API}/extension-tokens", json={"user_id": str(uuid.uuid4())}, headers=CSRF
    )
    assert response.status_code == 422


def test_bearer_token_authenticates_the_extension(
    client: TestClient, alice: dict[str, str]
) -> None:
    token = create_token(client)["token"]
    client.cookies.clear()
    response = client.get("/_probe/token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"user_id": alice["id"], "via": "token"}


def test_bearer_requests_do_not_need_the_csrf_header(
    client: TestClient, alice: dict[str, str]
) -> None:
    token = create_token(client)["token"]
    client.cookies.clear()
    response = client.post("/_probe/echo", json={}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_extension_token_cannot_use_dashboard_endpoints(
    client: TestClient, alice: dict[str, str]
) -> None:
    token = create_token(client)["token"]
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}", **CSRF}
    assert client.post(f"{API}/extension-tokens", json={}, headers=bearer).status_code == 401
    assert client.get("/_probe/cookie", headers=bearer).status_code == 401


def test_session_cookie_is_not_accepted_where_only_a_token_is(
    client: TestClient, alice: dict[str, str]
) -> None:
    assert client.get("/_probe/token").status_code == 401


# ------------------------------------------------------------------ revocation


def test_revoked_token_stops_working(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    created = create_token(client)
    bearer = {"Authorization": f"Bearer {created['token']}"}
    assert client.get("/_probe/token", headers=bearer).status_code == 200

    assert client.delete(f"{API}/extension-tokens/{created['id']}", headers=CSRF).status_code == 204

    assert client.get("/_probe/token", headers=bearer).status_code == 401
    assert client.get("/_probe/any", headers=bearer).status_code == 401
    row = db.get(ExtensionToken, uuid.UUID(created["id"]))
    assert row is not None and row.revoked_at is not None


def test_revoked_token_does_not_fall_back_to_the_cookie(
    client: TestClient, alice: dict[str, str]
) -> None:
    created = create_token(client)
    client.delete(f"{API}/extension-tokens/{created['id']}", headers=CSRF)
    # The client still holds a valid session cookie; the bad token must still be final.
    response = client.get("/_probe/any", headers={"Authorization": f"Bearer {created['token']}"})
    assert response.status_code == 401


def test_revocation_is_per_token(client: TestClient, alice: dict[str, str]) -> None:
    laptop = create_token(client, "laptop")
    desktop = create_token(client, "desktop")
    client.delete(f"{API}/extension-tokens/{laptop['id']}", headers=CSRF)
    ok = client.get("/_probe/token", headers={"Authorization": f"Bearer {desktop['token']}"})
    assert ok.status_code == 200


def test_revoking_twice_keeps_the_first_timestamp(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    created = create_token(client)
    url = f"{API}/extension-tokens/{created['id']}"
    assert client.delete(url, headers=CSRF).status_code == 204
    first = db.execute(text("SELECT revoked_at FROM extension_tokens")).scalar_one()
    assert client.delete(url, headers=CSRF).status_code == 204
    db.expire_all()
    assert db.execute(text("SELECT revoked_at FROM extension_tokens")).scalar_one() == first


def test_revoking_an_unknown_token_is_404(client: TestClient, alice: dict[str, str]) -> None:
    assert client.delete(f"{API}/extension-tokens/{uuid.uuid4()}", headers=CSRF).status_code == 404
    assert client.delete(f"{API}/extension-tokens/not-a-uuid", headers=CSRF).status_code == 422


def test_revocation_needs_csrf_header(client: TestClient, alice: dict[str, str]) -> None:
    created = create_token(client)
    assert client.delete(f"{API}/extension-tokens/{created['id']}").status_code == 403


# ------------------------------------------------------------------ cross-user access


@pytest.fixture
def bob_client(client: TestClient, db: Session) -> TestClient:
    other = TestClient(client.app, base_url="http://localhost")
    register(other, db, "bob@example.test")
    assert login(other, "bob@example.test").status_code == 200
    return other


def test_cannot_revoke_another_users_token(
    client: TestClient, db: Session, alice: dict[str, str], bob_client: TestClient
) -> None:
    alices = create_token(client)
    response = bob_client.delete(f"{API}/extension-tokens/{alices['id']}", headers=CSRF)
    assert response.status_code == 404  # same answer as for a token that does not exist
    assert (
        response.json()
        == bob_client.delete(f"{API}/extension-tokens/{uuid.uuid4()}", headers=CSRF).json()
    )
    row = db.get(ExtensionToken, uuid.UUID(alices["id"]))
    assert row is not None and row.revoked_at is None
    bearer = {"Authorization": f"Bearer {alices['token']}"}
    assert client.get("/_probe/token", headers=bearer).status_code == 200


def test_each_credential_resolves_to_its_own_user(
    client: TestClient, alice: dict[str, str], bob_client: TestClient
) -> None:
    alice_token = create_token(client)["token"]
    bob_token = create_token(bob_client)["token"]
    anonymous = TestClient(client.app, base_url="http://localhost")

    def who(token: str) -> str:
        r = anonymous.get("/_probe/token", headers={"Authorization": f"Bearer {token}"})
        return str(r.json()["user_id"])

    assert who(alice_token) == alice["id"]
    assert who(bob_token) != alice["id"]
    assert bob_client.get("/_probe/cookie").json()["user_id"] == who(bob_token)


def test_user_id_in_the_request_body_is_ignored(
    client: TestClient, alice: dict[str, str], bob_client: TestClient
) -> None:
    bob_id = bob_client.get("/_probe/cookie").json()["user_id"]
    response = client.post("/_probe/echo", json={"user_id": bob_id, "userId": bob_id}, headers=CSRF)
    assert response.status_code == 200
    assert response.json()["user_id"] == alice["id"] != bob_id
    assert response.json()["ignored_body_keys"] == ["userId", "user_id"]


def test_bobs_session_cookie_never_acts_as_alice(
    client: TestClient, alice: dict[str, str], bob_client: TestClient
) -> None:
    assert client.get("/_probe/cookie").json()["user_id"] == alice["id"]
    assert bob_client.get("/_probe/cookie").json()["user_id"] != alice["id"]


def test_deleting_a_user_invalidates_their_credentials(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    token = create_token(client)["token"]
    db.execute(text("DELETE FROM users"))
    db.commit()
    assert client.get("/_probe/cookie").status_code == 401
    assert (
        client.get("/_probe/token", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )
    assert db.query(Invite).filter(Invite.used_by.is_not(None)).count() == 0  # invite went too

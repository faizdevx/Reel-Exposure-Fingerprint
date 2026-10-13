from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_is_ok_without_a_database() -> None:
    # The configured URL points nowhere; liveness must not depend on it.
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_is_404() -> None:
    assert TestClient(create_app()).get("/api/v1/nope").status_code == 404

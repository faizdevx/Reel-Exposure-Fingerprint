"""Security audit H-4: a hosted deployment has TLS, secure cookies and a safe default.

Settings and cookie tests are real; the deployment files are checked as text so the tests need
neither Docker nor PyYAML (CI also runs `docker compose config` on the hosted override).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session
from support import CSRF, register

from app.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
DB = "postgresql+psycopg://u:p@localhost/x"


def settings(**kw: object) -> Settings:
    return Settings(database_url=DB, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the settings


def test_production_cookies_are_secure_by_default() -> None:
    assert settings(environment="production").secure_cookies is True


def test_local_and_ci_stay_plain_http_for_localhost() -> None:
    assert settings(environment="local").secure_cookies is False
    assert settings(environment="ci").secure_cookies is False


def test_production_refuses_to_start_with_insecure_cookies() -> None:
    with pytest.raises(ValidationError, match="REF_COOKIE_SECURE"):
        settings(environment="production", cookie_secure=False)


def test_production_with_an_explicit_true_is_fine_and_local_may_opt_in() -> None:
    assert settings(environment="production", cookie_secure=True).secure_cookies is True
    assert settings(environment="local", cookie_secure=True).secure_cookies is True


def test_the_environment_variable_form_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REF_DATABASE_URL", DB)
    monkeypatch.setenv("REF_ENVIRONMENT", "production")
    monkeypatch.setenv("REF_COOKIE_SECURE", "false")
    with pytest.raises(ValidationError):
        Settings()


# ------------------------------------------------------------------ the cookie, on the wire


@pytest.mark.db
def test_a_production_login_sets_a_secure_httponly_samesite_cookie(
    client: TestClient, db: Session
) -> None:
    client.app.dependency_overrides[get_settings] = lambda: settings(environment="production")  # type: ignore[attr-defined]
    register(client, db, "prod@example.test")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "prod@example.test", "password": "correct horse battery staple"},
        headers=CSRF,
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "ref_session=" in cookie
    for flag in ("secure", "httponly", "samesite=lax", "path=/"):
        assert flag in cookie, flag


@pytest.mark.db
def test_the_local_cookie_is_not_secure_so_http_localhost_still_works(
    client: TestClient, db: Session
) -> None:
    register(client, db, "local@example.test")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "local@example.test", "password": "correct horse battery staple"},
        headers=CSRF,
    )
    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie.replace("samesite", "") and "httponly" in cookie


# ------------------------------------------------------------------ the deployment files


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_base_compose_publishes_nothing_but_loopback() -> None:
    ports = re.findall(r'^\s*-\s*"([^"]+:\d+)"', read("docker-compose.yml"), re.M)
    assert ports, "expected published ports"
    assert all(p.startswith("127.0.0.1:") for p in ports), ports
    assert "caddy" not in read("docker-compose.yml")


def test_the_hosted_override_turns_on_production_tls_and_closes_the_dashboard_port() -> None:
    text = read("docker-compose.hosted.yml")
    assert "REF_ENVIRONMENT: production" in text
    assert 'REF_COOKIE_SECURE: "true"' in text
    assert re.search(r"dashboard:\s*\n\s*#.*\n\s*ports: !reset \[\]", text), "dashboard port closed"
    assert "image: caddy:2" in text and ":latest" not in text
    assert '"80:80"' in text and '"443:443"' in text
    assert "REF_DOMAIN: ${REF_DOMAIN:?" in text  # a hosted start without a name fails loudly
    published = re.findall(r'^\s*-\s*"(\d+:\d+)"', text, re.M)
    assert sorted(published) == ["443:443", "80:80"]  # nothing else is public


def test_the_caddyfile_terminates_tls_with_hsts_and_forwards_to_the_dashboard_only() -> None:
    text = read("Caddyfile")
    assert text.count("{$REF_DOMAIN}") == 1
    assert 'Strict-Transport-Security "max-age=' in text
    assert 'X-Content-Type-Options "nosniff"' in text and 'X-Frame-Options "DENY"' in text
    assert re.search(r"max_size\s+1MB", text)
    assert "reverse_proxy dashboard:80" in text
    assert "http://" not in re.sub(r"#.*", "", text)  # no plain-HTTP site block


def test_nginx_limits_bodies_and_rates_and_takes_the_client_from_the_right() -> None:
    text = read("dashboard/nginx.conf")
    assert re.search(r"client_max_body_size\s+1m;", text)
    assert "limit_req_zone $binary_remote_addr" in text and "limit_req zone=" in text
    assert "limit_req_status 429;" in text
    # only private networks may vouch for X-Forwarded-For, and the chain is read from the right
    real_ips = re.findall(r"set_real_ip_from\s+(\S+);", text)
    assert real_ips and all(n.startswith(("10.", "172.16.", "192.168.")) for n in real_ips), (
        real_ips
    )
    assert "real_ip_recursive on;" in text
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in text
    assert "$http_x_forwarded_for;" not in text.replace("$proxy_add_x_forwarded_for", "")


def test_compose_gives_the_api_the_proxy_networks_it_may_trust() -> None:
    text = read("docker-compose.yml")
    assert "REF_TRUSTED_PROXIES" in text
    assert "0.0.0.0/0" not in text  # never "everyone is a proxy"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_docker_compose_accepts_the_hosted_override_and_requires_a_domain() -> None:
    base = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.hosted.yml"]
    env = {k: v for k, v in os.environ.items() if k != "REF_DOMAIN"} | {"POSTGRES_PASSWORD": "x"}
    ok = subprocess.run(  # noqa: S603
        [*base, "config", "--quiet"],
        cwd=ROOT,
        env={**env, "REF_DOMAIN": "ref.example.org"},
        capture_output=True,
        text=True,
        check=False,
    )
    if ok.returncode != 0 and "unknown" in ok.stderr.lower():
        pytest.skip("this docker compose does not know !reset")
    assert ok.returncode == 0, ok.stderr
    missing = subprocess.run(  # noqa: S603
        [*base, "config", "--quiet"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert missing.returncode != 0 and "REF_DOMAIN" in missing.stderr

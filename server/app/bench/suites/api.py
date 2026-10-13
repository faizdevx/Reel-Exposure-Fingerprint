"""Dashboard API latency (N4: p95 under 500 ms) and event upload latency, at the spec's capacity.

The database is seeded to the capacity of N11: 5 users and 100,000 exposures in total (20,000 each,
in 200 sessions over 100 days), with a topic distribution for every Reel (28 topics, one row each,
per user: ADR-024), the fingerprints and snapshots rebuilt by the real engine and the predictions
rebuilt by the real replay. The API is then started as its own process (uvicorn, one worker, the
database on the same machine, pinned to the CPUs given) and measured over HTTP: sequential requests
from one user, and five users at once. Rate limits are raised for the run, otherwise the limiter, not
the endpoint, would be measured (`REF_*_PER_MINUTE`); everything else is the production code path.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psutil
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.bench import data
from app.bench.stats import summarize
from app.bench.suites import database

CSRF = {"X-Requested-With": "ref-dashboard"}
PASSWORD = "benchmark password 123"
HIGH = "1000000"
LIMIT_ENV = {
    f"REF_{name}": HIGH
    for name in (
        "INGEST_PER_MINUTE",
        "TOKEN_PER_MINUTE",
        "DASHBOARD_PER_MINUTE",
        "DESTRUCTIVE_PER_MINUTE",
        "API_IP_PER_MINUTE",
        "AUTH_IP_PER_MINUTE",
    )
} | {
    f"REF_{name}": HIGH
    for name in (
        "INGEST_BURST",
        "TOKEN_BURST",
        "DASHBOARD_BURST",
        "DESTRUCTIVE_BURST",
        "API_IP_BURST",
        "AUTH_IP_BURST",
        "LOGIN_FAILURES_PER_EMAIL",
    )
}


# ---------------------------------------------------------------------------------- seeding


def _copy(engine: Engine, statement: str, rows: Any) -> int:
    n = 0
    raw = engine.raw_connection()
    try:
        cursor: Any = raw.cursor()
        with cursor, cursor.copy(statement) as copy:
            for row in rows:
                copy.write_row(row)
                n += 1
        raw.commit()
    finally:
        raw.close()
    return n


def database_description(url: str) -> dict[str, str]:
    """Which PostgreSQL the numbers were measured on (the server, not the driver)."""
    engine = database.engine_for(url)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar_one()
        vector = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        buffers = conn.execute(text("SHOW shared_buffers")).scalar_one()
    return {
        "server": str(version).split(",")[0],
        "pgvector": str(vector),
        "shared_buffers": str(buffers),
    }


def seed(url: str, *, users: int, reels_per_user: int, taxonomy_file: Path) -> dict[str, Any]:
    from app.fingerprint.engine import FingerprintConfig
    from app.fingerprint.store import rebuild_user
    from app.prediction.config import PredictionConfig
    from app.prediction.store import rebuild_predictions
    from app.security import hash_password
    from app.taxonomy import load_taxonomy
    from app.worker.store import sync_topics

    engine = database.reset(url)
    taxonomy = load_taxonomy(taxonomy_file)
    topic_ids = [t.id for t in taxonomy.topics]
    with Session(engine) as db:
        sync_topics(db, taxonomy)
        db.commit()

    started = time.perf_counter()
    password_hash = hash_password(PASSWORD)
    rng = np.random.default_rng(0)
    sessions_per_user = 200
    per_session = reels_per_user // sessions_per_user
    first_day = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    version = "bench-assignments"
    user_rows, consent_rows, session_rows, exposure_rows, assignment_rows = [], [], [], [], []
    ids: list[uuid.UUID] = []
    for u in range(users):
        uid = uuid.UUID(int=(1 << 100) + u)
        ids.append(uid)
        user_rows.append((uid, f"bench{u}@example.test", password_hash))
        consent_rows.append((uid, "bench", first_day, None))
        prior = rng.dirichlet(np.full(len(topic_ids), 0.4))
        for s in range(sessions_per_user):
            sid = uuid.UUID(int=(2 << 100) + u * 10_000 + s)
            day = first_day + timedelta(days=s * 100 // sessions_per_user, hours=(s % 2) * 6)
            session_rows.append((sid, uid, day, day + timedelta(minutes=45), "stopped", "user"))
            for j in range(per_session):
                n = s * per_session + j
                code = f"B{u}x{n:06d}"
                exposure_rows.append(
                    (
                        uuid.UUID(int=(3 << 100) + u * 1_000_000 + n),
                        sid,
                        uid,
                        code,
                        day + timedelta(seconds=j * 25),
                        3000 + (n * 131) % 12000,
                        15000,
                        f"creator{n % 97}",
                        f"caption {n} about something #tag{n % 13}",
                        "{" + f"tag{n % 13}" + "}",
                        json.dumps(
                            {"caption": "available", "caption_truncated": False, "frame_count": 0}
                        ),
                        "bench",
                    )
                )
                probs = rng.dirichlet(prior * 40 + 0.05)
                for tid, p in zip(topic_ids, probs, strict=True):
                    assignment_rows.append((uid, code, version, tid, float(p), None, float(p)))
        # flush per user to keep memory bounded
        if len(assignment_rows) > 400_000:
            _flush(engine, user_rows, consent_rows, session_rows, exposure_rows, assignment_rows)
    _flush(engine, user_rows, consent_rows, session_rows, exposure_rows, assignment_rows)
    insert_s = time.perf_counter() - started

    rebuild = {}
    for u, uid in enumerate(ids):
        with Session(engine) as db:
            t0 = time.perf_counter()
            result = rebuild_user(
                db, uid, assignment_version=version, topic_ids=topic_ids, config=FingerprintConfig()
            )
            db.commit()
            t1 = time.perf_counter()
            predictions = rebuild_predictions(
                db,
                uid,
                assignment_version=version,
                fp_config=FingerprintConfig(),
                config=PredictionConfig(),
            )
            db.commit()
            t2 = time.perf_counter()
        rebuild[f"user{u}"] = {
            "fingerprint_rebuild_s": round(t1 - t0, 2),
            "prediction_rebuild_s": round(t2 - t1, 2),
            "snapshots": len(result.snapshots),
            "prediction_rows": predictions.prediction_rows,
        }
    with engine.connect() as conn:
        sizes = {
            row[0]: {"rows": int(row[1]), "total_mb": round(row[2] / 2**20, 1)}
            for row in conn.execute(
                text(
                    "SELECT relname, n_live_tup, pg_total_relation_size(relid) FROM pg_stat_user_tables "
                    "ORDER BY pg_total_relation_size(relid) DESC LIMIT 8"
                )
            )
        }
        conn.execute(text("ANALYZE"))
        conn.commit()
    return {
        "users": users,
        "exposures": users * reels_per_user,
        "topics": len(topic_ids),
        "sessions_per_user": sessions_per_user,
        "insert_seconds": round(insert_s, 1),
        "rebuild": rebuild,
        "tables": sizes,
        "user_ids": [str(i) for i in ids],
    }


def _flush(
    engine: Engine,
    users: list[Any],
    consents: list[Any],
    sessions: list[Any],
    exposures: list[Any],
    assignments: list[Any],
) -> None:
    if users:
        _copy(engine, "COPY users (id, email, password_hash) FROM STDIN", users)
        _copy(
            engine,
            "COPY consents (user_id, policy_version, granted_at, revoked_at) FROM STDIN",
            consents,
        )
    if sessions:
        _copy(
            engine,
            "COPY sessions (id, user_id, started_at, ended_at, status, stop_reason) FROM STDIN",
            sessions,
        )
    if exposures:
        _copy(
            engine,
            "COPY reel_exposures (event_id, session_id, user_id, reel_shortcode, observed_at, dwell_ms, "
            "video_duration_ms, creator_handle, caption, hashtags, field_status, adapter_version) FROM STDIN",
            exposures,
        )
    if assignments:
        _copy(
            engine,
            "COPY topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, p_text, "
            "p_visual, p_fused) FROM STDIN",
            assignments,
        )
    for rows in (users, consents, sessions, exposures, assignments):
        rows.clear()


# ---------------------------------------------------------------------------------- the server


class Server:
    def __init__(self, url: str, port: int, cpus: list[int] | None) -> None:
        self.url, self.port, self.cpus = url, port, cpus
        self.process: subprocess.Popen[bytes] | None = None
        self.frames = tempfile.mkdtemp(prefix="ref-bench-frames-")

    def __enter__(self) -> "Server":
        env = {
            **os.environ,
            **LIMIT_ENV,
            "REF_DATABASE_URL": self.url,
            "REF_ENVIRONMENT": "ci",
            "REF_FRAME_STORE_DIR": self.frames,
            "REF_FRAME_SWEEP_INTERVAL_SECONDS": "0",
            "REF_LOG_LEVEL": "WARNING",
        }
        self.process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=database.SERVER_DIR,
            env=env,
        )
        if self.cpus:
            root = psutil.Process(self.process.pid)
            for proc in [root, *root.children(recursive=True)]:
                proc.cpu_affinity(self.cpus)
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.5)
        raise RuntimeError("the API did not start")

    def __exit__(self, *exc: object) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=30)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def rss_mb(self) -> float:
        assert self.process is not None
        root = psutil.Process(self.process.pid)  # the launcher may be a thin parent of the server
        total = root.memory_info().rss + sum(
            c.memory_info().rss for c in root.children(recursive=True)
        )
        return float(round(total / 2**20, 1))


def _login(base: str, email: str) -> httpx.Client:
    client = httpx.Client(base_url=base, timeout=30)
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF
    )
    response.raise_for_status()
    return client


def _measure(
    client: httpx.Client, path: str, n: int, warmup: int = 15
) -> tuple[list[float], int, int]:
    for _ in range(warmup):
        client.get(path)
    times, errors, size = [], 0, 0
    for _ in range(n):
        t0 = time.perf_counter()
        response = client.get(path)
        times.append((time.perf_counter() - t0) * 1000.0)
        size = len(response.content)
        if response.status_code != 200:
            errors += 1
    return times, errors, size


def dashboard(
    seeded: dict[str, Any],
    url: str,
    *,
    n: int,
    cpus: list[int] | None,
    port: int,
    clients: int | None = None,
) -> dict[str, Any]:
    """`clients` logins at once, round robin over the seeded users (default: one per user)."""
    count = clients or seeded["users"]
    emails = [f"bench{c % seeded['users']}@example.test" for c in range(count)]
    endpoints = {
        "GET /fingerprint/current (exposure)": "/api/v1/fingerprint/current?kind=exposure",
        "GET /fingerprint/current (engagement)": "/api/v1/fingerprint/current?kind=engagement",
        "GET /fingerprint/history (day, 90)": "/api/v1/fingerprint/history?kind=exposure&bucket=day&limit=90",
        "GET /fingerprint/history (day, 366)": "/api/v1/fingerprint/history?kind=exposure&bucket=day&limit=366",
        "GET /fingerprint/history (session, 90)": "/api/v1/fingerprint/history?kind=engagement&bucket=session&limit=90",
        "GET /sessions": "/api/v1/sessions",
        "GET /topics/{id}/evidence (first page)": "/api/v1/topics/{top}/evidence?kind=exposure&limit=20&offset=0",
        "GET /topics/{id}/evidence (offset 5000)": "/api/v1/topics/{top}/evidence?kind=exposure&limit=20&offset=5000",
        "GET /topics/{id}/evidence (engagement)": "/api/v1/topics/{top}/evidence?kind=engagement&limit=100&offset=0",
        "GET /predictions/next": "/api/v1/predictions/next",
        "GET /self-report": "/api/v1/self-report",
    }
    out: dict[str, Any] = {}
    with Server(url, port, cpus) as server:
        logins = [_login(server.base, e) for e in emails]
        top = {}
        for i, client in enumerate(logins):  # a topic with real weight for this user
            body = client.get("/api/v1/fingerprint/current?kind=exposure").json()
            top[i] = body["topics"][0]["topic_id"]
        for name, template in endpoints.items():
            paths = [template.replace("{top}", str(top[i])) for i in range(len(logins))]
            seq, errors, size = _measure(logins[0], paths[0], n)
            results: list[list[float]] = [[] for _ in logins]
            errs = [0] * len(logins)

            def work(
                i: int,
                paths: list[str] = paths,
                results: list[list[float]] = results,
                errs: list[int] = errs,
            ) -> None:
                times, e, _ = _measure(logins[i], paths[i], n)
                results[i], errs[i] = times, e

            threads = [threading.Thread(target=work, args=(i,)) for i in range(len(logins))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            out[name] = {
                "response_bytes": size,
                "sequential_ms": summarize(seq),
                "five_users_at_once_ms": summarize([x for r in results for x in r]),
                "errors": errors + sum(errs),
            }
        out["_server_rss_mb"] = server.rss_mb()
        out["_clients"] = len(logins)
        for c in logins:
            c.close()
    return out


# ---------------------------------------------------------------------------------- ingest


def ingest(url: str, *, cpus: list[int] | None, port: int, reps: int) -> dict[str, Any]:
    from app.security import new_secret, sha256_digest

    engine = database.engine_for(url)
    token = new_secret()
    user = uuid.UUID(int=(9 << 100))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES (:i, 'ingest@example.test', 'x') ON CONFLICT DO NOTHING"
            ),
            {"i": user},
        )
        conn.execute(
            text("INSERT INTO extension_tokens (id, user_id, token_sha256) VALUES (:i, :u, :h)"),
            {"i": uuid.uuid4(), "u": user, "h": sha256_digest(token)},
        )
    headers = {"Authorization": f"Bearer {token}"}
    rng = random.Random(0)  # noqa: S311
    out: dict[str, Any] = {}
    with (
        Server(url, port, cpus) as server,
        httpx.Client(base_url=server.base, timeout=60, headers=headers) as client,
    ):
        client.put("/api/v1/consent", json={"policy_version": "bench"}).raise_for_status()
        session_id = client.post("/api/v1/sessions").json()["id"]
        captions = data.captions(5000)

        def event(i: int, frames: int = 0) -> dict[str, Any]:
            caption, tags = captions[i % len(captions)]
            return {
                "event_id": str(uuid.uuid4()),
                "session_id": session_id,
                "reel_shortcode": f"I{i:06d}x{rng.randint(0, 9999):04d}",
                "observed_at": datetime.now(UTC).isoformat(),
                "dwell_ms": 8000,
                "video_duration_ms": 15000,
                "video_duration_status": "available",
                "creator_handle": {"value": "somecreator", "status": "available"},
                "caption": {"value": caption, "status": "available"},
                "caption_truncated": False,
                "hashtags": {"value": list(tags), "status": "available"},
                "frame_count": frames,
                "adapter_version": "bench",
            }

        counter = 0
        for size in (1, 10, 50):
            times, body_bytes = [], 0
            for _ in range(reps + 5):
                events = [event(counter + k) for k in range(size)]
                counter += size
                payload = json.dumps({"events": events})
                body_bytes = len(payload)
                t0 = time.perf_counter()
                response = client.post(
                    "/api/v1/events", content=payload, headers={"Content-Type": "application/json"}
                )
                times.append((time.perf_counter() - t0) * 1000.0)
                response.raise_for_status()
            out[f"POST /events, batch of {size}"] = {
                "body_bytes": body_bytes,
                "ms": summarize(times[5:]),
            }

        # a replay of an already stored batch (retry after a lost reply)
        events = [event(counter + k) for k in range(50)]
        counter += 50
        payload = json.dumps({"events": events})
        client.post(
            "/api/v1/events", content=payload, headers={"Content-Type": "application/json"}
        ).raise_for_status()
        replay = []
        for _ in range(reps):
            t0 = time.perf_counter()
            client.post(
                "/api/v1/events", content=payload, headers={"Content-Type": "application/json"}
            ).raise_for_status()
            replay.append((time.perf_counter() - t0) * 1000.0)
        out["POST /events, replay of 50 stored events"] = {"ms": summarize(replay)}

        frames = data.load_frames(None, 5)
        times, upload_bytes = [], 0
        for k in range(reps + 3):
            body = event(counter + k, frames=5)
            client.post("/api/v1/events", json={"events": [body]}).raise_for_status()
            files = [("frames", (f"f{j}.jpg", frames[j], "image/jpeg")) for j in range(5)]
            upload_bytes = sum(len(f) for f in frames)
            t0 = time.perf_counter()
            response = client.post(f"/api/v1/events/{body['event_id']}/frames", files=files)
            times.append((time.perf_counter() - t0) * 1000.0)
            response.raise_for_status()
        out["POST /events/{id}/frames, 5 frames"] = {
            "jpeg_bytes": upload_bytes,
            "ms": summarize(times[3:]),
        }
        out["_server_rss_mb"] = server.rss_mb()
    return out

# Deployment

Sources: spec §5, §6, §7, N3, N6, N11, M7 and the security rules in `CONTRIBUTING.md`. Target: one machine, one command. Capacity: ≤5 users and ≤100k exposures (N11).

## Components

| Service | Role |
|---|---|
| `db` | PostgreSQL 16 + pgvector (data, vectors, `jobs` queue) |
| `api` | FastAPI: auth, ingest, read APIs |
| `worker` | One Python process; models loaded once; OCR, text and visual embeddings, fusion, fingerprints, prediction |
| `dashboard` | Static React SPA build |
| `caddy` | TLS reverse proxy, hosted mode only |
| Frame volume | Local docker volume, shared by `api` and `worker`; files live <24 h |

The extension is built separately (`extension/`) and loaded in Chrome. It is not deployed by compose.

## Modes

| Mode | Binding | Users | TLS |
|---|---|---|---|
| Local single-user | `127.0.0.1` | One seeded user | Not required on localhost |
| Hosted (optional) | Public via Caddy | Invite-only accounts | Required |

Hosted mode exists only if the owner chooses cohort use (decision D-1). Same code in both modes.

## Run

```
docker compose up
```

M7 delivers the one-command setup: seeded user, README, and privacy notice text.

## Configuration

All configuration comes from environment variables, read through one settings module per app. Never hardcode values.

- Secrets (DB password, session secret, TLS settings) live only in environment variables.
- Commit `.env.example` with placeholders. Never commit `.env` or real secrets.
- Never log secrets.

Variables (see `.env.example`):

| Variable | Used by | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | compose (`db`, `api`) | Required. Compose refuses to start without it |
| `REF_DATABASE_URL` | server | Required, no default. Compose builds it from `POSTGRES_PASSWORD` |
| `REF_ENVIRONMENT` | server | `local` (default), `ci`, `production` |
| `REF_LOG_LEVEL` | server | `INFO` by default |
| `REF_FRAME_STORE_DIR` | server | Frame volume path (local volume, no object storage) |
| `REF_FRAME_TTL_HOURS` | server | Frame retention, 1 to 24 (default 24; a longer value is refused) |
| `REF_FRAME_SWEEP_INTERVAL_SECONDS` | server | How often the API deletes expired frames (default 900; 0 turns it off) |
| `REF_SESSION_TTL_HOURS` | server | Dashboard session lifetime; default 168 |
| `REF_INVITE_TTL_HOURS` | server | Default invite lifetime for the CLI; default 72 |
| `REF_COOKIE_SECURE` | server | `true`/`false`. Default: Secure cookies in `production`, off otherwise. `REF_ENVIRONMENT=production` with `REF_COOKIE_SECURE=false` is refused at startup |
| `REF_DOMAIN` | compose (hosted override) | Public name for TLS; required by `docker-compose.hosted.yml` |
| `REF_TRUSTED_PROXIES` | server | Networks whose `X-Forwarded-For` is believed (compose: the private ranges). Empty: the peer is the client |
| `REF_INGEST_PER_MINUTE`, `REF_INGEST_BURST`, `REF_TOKEN_*`, `REF_DASHBOARD_*`, `REF_DESTRUCTIVE_*`, `REF_API_IP_*`, `REF_AUTH_IP_*`, `REF_LOGIN_FAILURES_PER_EMAIL`, `REF_LOGIN_FAILURE_REFILL_SECONDS`, `REF_PASSWORD_CONCURRENCY` | server | Rate limits; defaults in `docs/api.md` ("Limits and error shapes") |
| `VITE_REF_API_BASE` | extension build | Server address the extension is built for (HTTPS unless loopback) |
| `TEST_DATABASE_URL` | pytest | Optional; enables the database tests |

Add every new variable to `.env.example` and to this table.

### Extension to server

The address is fixed when the extension is built (`VITE_REF_API_BASE`, default `http://127.0.0.1:8000`; `extension/src/api-base.ts`). Plain HTTP is accepted only for `127.0.0.1`, `localhost` and `::1`; any other server must be HTTPS, otherwise the build fails. The manifest asks for host access to Instagram and to that server only (the default build keeps `127.0.0.1` and `localhost`), so no CORS setup is needed. Build for a hosted server with `VITE_REF_API_BASE=https://ref.example.org npm run build` in `extension/`. The consent text shows the address the build was made for.

### Hosted mode (TLS)

```
REF_DOMAIN=ref.example.org docker compose -f docker-compose.yml -f docker-compose.hosted.yml up -d
```

`docker-compose.hosted.yml` adds Caddy (`Caddyfile`), which gets and renews the certificate for `REF_DOMAIN` (the name must point at the machine, ports 80 and 443 must be reachable), sends HSTS, `nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`, caps bodies at 1 MB and forwards to the dashboard container (which proxies `/api/` to the API). It sets `REF_ENVIRONMENT=production` and `REF_COOKIE_SECURE=true` for the API (the API refuses to start in production with insecure cookies), closes the dashboard's loopback port, and starts only with `REF_DOMAIN` set. Only Caddy publishes ports (80, 443). Build the extension for the same address (above).

Client addresses and rate limits: Caddy sets `X-Forwarded-For` to the address it saw, nginx appends to it, and the API trusts the header only from `REF_TRUSTED_PROXIES` (compose sets the private ranges) and reads it from the right. In local mode anyone who can reach the published loopback port is already on the machine and can choose their own address key; hosted mode closes that port.

Not built yet: nightly `pg_dump` and its retention, the worker service, and a check that the certificate is valid. Hosted mode has not been run against a real domain in this repository; the files are validated by `docker compose config`, `nginx -t` and tests, and the nginx rules were exercised against a stand-in API.

### Creating the first user

Accounts are invite-only. On the host running the stack:

```
docker compose exec api python -m app.manage create-invite
```

The code is printed once and valid for `REF_INVITE_TTL_HOURS`. Register with `POST /api/v1/auth/register` (`docs/api.md`).

## Sizing

- Host: 4 vCPU, 8 GB RAM, no GPU (spec §11 M7, N3).
- Throughput target: ≥1 Reel per 3 s; a 200-Reel session processed within 10 minutes.
- If N3 is missed, use ONNX / int8 for SigLIP and e5, then re-check accuracy on the label set.
- No vector index until >100k rows.

## Operations

- **Migrations:** run Alembic on startup or as a one-off job. Never edit an applied migration.
- **Frame retention:** the worker deletes frames after extraction. The API sweeps every 15 minutes and removes any frame older than the TTL by modification time; `python -m app.manage sweep-frames` does the same once, for cron. Verify the frame directory is empty the day after a session.
- **Observability (N10):** structured logs and `GET /api/v1/stats/pipeline` (queue depth, failures per stage, latency per stage). Logs contain IDs and error classes only.
- **Health:** `GET /healthz`.
- **Backups (hosted):** nightly `pg_dump`. Backups contain user data, so the retention window must be documented in the privacy notice.
- **Model files:** download at build or first run. Verify model IDs and licences on Hugging Face at build time. Do not commit weights.

## Security checklist

- TLS on anything not bound to localhost (Caddy).
- Payload size limits and ingest rate limiting enabled.
- API reachable only through Caddy in hosted mode; DB not exposed publicly.
- Extension tokens revocable from the dashboard; revocation tested.
- No Instagram credentials, cookies or tokens anywhere in the stack.

## Open issues

Not yet decided (see `docs/architecture-decisions.md`, status Proposed):

- Rate-limit mechanism and values (ADR-005).
- CORS origins for dashboard and extension (ADR-004).
- Backup handling relative to deletion (ADR-012).
- Self-only versus cohort, which decides whether hosted mode is built at all (D-1, ADR-019).

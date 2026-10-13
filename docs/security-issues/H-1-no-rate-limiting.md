[HIGH] No rate limiting or throttling anywhere (spec N6, ADR-005)

**Fixed on 2026-10-09** in `server/app/ratelimit.py` and the places listed in `docs/security-audit.md` (H-1). Tests: `tests/test_rate_limit.py`.

**Severity:** HIGH. **Area:** API abuse, authentication. **Source:** `docs/security-audit.md`, H-1.

## Problem

Spec N6 requires a rate limit on ingest. ADR-005 accepts 60 requests per minute on `/events` and `/frames` (burst 20, `429` with `Retry-After`). Nothing is implemented: no limiter in `server/app/main.py`, none in `dashboard/nginx.conf`, and no login throttling or lockout.

Probe on 2026-10-09: 200 consecutive `POST /api/v1/auth/login` requests returned the same status every time and never `429`.

## Impact

- Online password guessing and credential stuffing against `/auth/login` cost the attacker nothing but time.
- Each login attempt, including for an unknown email, costs the server an Argon2id verification (library default, 64 MiB, 4 lanes). A few dozen parallel requests exhaust a small VPS: unauthenticated denial of service.
- Any extension-token holder can call `/events`, `/frames`, `/sessions` and `/consent` at full speed (unbounded rows, jobs and frame files; see the quotas finding M-10).
- `/auth/register` hashes the password before consuming the invite: an unauthenticated CPU cost.

## Suggested fix (not applied; the audit changed no code)

- Implement ADR-005 as accepted: token bucket per extension token on `/events` and `/frames`, `429` with `Retry-After`; make the extension's backoff honour it.
- Per-IP and per-email throttling on `/auth/login` and `/auth/register`, with a delay or temporary lock after repeated failures.
- A global cap on concurrent Argon2 verifications.
- `limit_req` / `limit_conn` in nginx. For per-IP limits the API must see the real client address (decide the trusted proxy, audit item L-8).

## Acceptance

- A burst above the limit gets `429` with `Retry-After`; a well-behaved client recovers after the window.
- Repeated failed logins for one email or from one address are slowed or refused, and a correct login still works for a different user.
- Tests for every limited route, including that one user's limit does not affect another's.

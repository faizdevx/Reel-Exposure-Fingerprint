[HIGH] Request bodies are unbounded on every route except events and frames (ADR-013)

**Fixed on 2026-10-09** in `server/app/body_limit.py` and `server/app/main.py` (H-2). Tests: `tests/test_body_limits.py`.

**Severity:** HIGH. **Area:** payload limits, API abuse, privacy leakage. **Source:** `docs/security-audit.md`, H-2.

## Problem

Only `POST /events` (256 KB, streamed and counted in `limited_body`) and the frame upload (800 KB, `BodyLimitMiddleware`, one rule in `server/app/main.py`) have a size limit. ADR-013 says every other endpoint is limited to 16 KB, enforced at the ASGI layer before parsing. ADR-013 is Proposed and was not applied.

Probes on 2026-10-09:

- A 20 MB JSON body to the unauthenticated `POST /api/v1/auth/login` was read and parsed in full (about 0.5 s) and answered `422`, not `413`.
- The default FastAPI `422` body echoes the submitted value. `POST /auth/register` with a short password and an invite code returned both (`"input": "short-pw"`). A 5 MB login request returned a 5,000,151 byte response, so the server reflects what it is sent. The events route strips values from its errors on purpose; the other routes do not.

## Impact

- Any outsider who can reach the API can make it buffer and parse arbitrarily large bodies on `/auth/login` and `/auth/register` (memory exhaustion). Uvicorn has no body limit by default.
- nginx's default 1 MB `client_max_body_size` protects only requests that pass through the dashboard container; the extension talks to the API port directly, and that is the port a hosted deployment would publish.
- Passwords and invite codes are reflected into error responses, where proxies and clients may log or cache them.

## Suggested fix (not applied)

- Apply `BodyLimitMiddleware` to all `/api/` routes with the 16 KB default of ADR-013 and the two existing exceptions.
- Set `client_max_body_size` in `dashboard/nginx.conf`.
- Replace FastAPI's default validation response with one that carries only locations and error types, as `parse_batch` in `events.py` already does.
- Consider uvicorn `--limit-concurrency`.
- Accept ADR-013 (or record why not).

## Acceptance

- A body over 16 KB to any route other than events and frames gets `413` without being read to the end.
- No `422` response contains a submitted value.
- A test per route family (auth, consent, sessions, tokens, survey, deletion).

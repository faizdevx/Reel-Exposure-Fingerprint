# Security and production-readiness audit

Date: 2026-10-09. **Re-audited the same day after the HIGH fixes (see "Re-audit after the fixes"); the sections below describe each finding as it was found and now carry its status.** Scope: the whole repository at the working tree of that day (server, dashboard, extension, compose files, CI, docs). Method: reading the code and the documents (`CONTRIBUTING.md`, `docs/final-spec.md`, `docs/architecture-decisions.md`, `docs/database.md`, `docs/api.md`, `docs/privacy.md`, `docs/testing-strategy.md`), a small set of read-only probes against the running application object (listed in the appendix), and `pip-audit` and `npm audit` on the runtime dependencies. **The first pass changed no code; the fixes came afterwards (below).** Nothing was run against a deployed system, and no penetration test was done.

## Verdict

**First pass:** not ready for any use beyond a single trusted user on localhost. No CRITICAL findings (no unauthenticated access to data, no remote code execution, no SQL injection, no way to read or change another user's rows through the API); **4 HIGH** (rate limiting, payload limits, shared cache, TLS and deployment), 10 MEDIUM, 11 LOW.

**After the fixes:** all 4 HIGH findings are fixed, each with regression tests, and the re-audit found no CRITICAL or HIGH finding. 10 MEDIUM remain open (none was in scope), and the LOW list grew by 3 items that the fixes introduced or exposed (L-12 to L-14). The system is now suitable for the local mode it was built for, and its hosted mode (TLS in front, secure cookies enforced, an HTTPS-only extension build) exists but has not been run against a real domain. The MEDIUM findings, above all M-1 (tokens), M-2 (consent re-grant), M-3 (re-authentication) and M-5 (parameters in logs), should be closed before a cohort that is not the developer uses it.

The authorization model is sound where it was audited: every query reads `user_id` from the credential, ownership is part of the query, another user's object looks like a missing one, and the ingest and deletion code take the locks they need. The weaknesses are around the edges: abuse resistance, deployment, defence in depth, and a few places where a document claims something the code does not do.

How the severities were chosen. The target is the spec's: at most 5 invited users, local mode first and possibly a hosted cohort (owner decision D-1). A finding is HIGH when a user or an outsider can cause real harm in that setting, or when a requirement of the spec is unmet and the gap is exploitable. Where a finding only matters once the system is hosted, the text says so. No finding is rated by how bad the worst imaginable case is.

| ID | Severity | Title |
|---|---|---|
| H-1 | HIGH, **fixed** | No rate limiting or throttling anywhere (spec N6, ADR-005) |
| H-2 | HIGH, **fixed** | Request bodies are unbounded on every route except events and frames (ADR-013) |
| H-3 | HIGH, **fixed** | One user can poison the shared Reel cache for every other user |
| H-4 | HIGH, **fixed** | No supported hosted deployment: no TLS, insecure cookie default, plaintext Bearer token |
| M-1 | MEDIUM | Extension tokens cannot be listed, have no expiry and no dashboard UI |
| M-2 | MEDIUM | Consent can be re-granted by the extension token alone, after a dashboard revocation |
| M-3 | MEDIUM | Irreversible actions need no re-authentication; weak session lifecycle |
| M-4 | MEDIUM | No security headers, no CSP, no clickjacking protection; API docs are public |
| M-5 | MEDIUM | Database errors put bound parameters (captions) into logs |
| M-6 | MEDIUM | The content script can read the extension token; `privacy.md` says it cannot |
| M-7 | MEDIUM | The application connects to PostgreSQL as a superuser |
| M-8 | MEDIUM | The job queue is filled but never consumed, claimed, retried or purged |
| M-9 | MEDIUM | Frames are validated by magic bytes only; no pixel cap, no disk quota |
| M-10 | MEDIUM | No per-user quotas: events, sessions, tokens, jobs, disk |
| L-1 to L-14 | LOW | See below (L-12 to L-14 are new) |

GitHub issues for the four HIGH findings: **not created, and no longer needed as open issues** (see "GitHub issues" at the end).

---

## HIGH

### H-1: No rate limiting or throttling anywhere

**Status: FIXED (2026-10-09).** Token-bucket limiter in `server/app/ratelimit.py`, charged where a credential is accepted (`app/auth_deps.py`, so a route cannot skip it) and by a router-level guard for every `/api/` request. Ingest is 60 a minute, burst 20 (ADR-005), per user, with events and frames in separate buckets, so extra tokens buy nothing. Login and register are limited per client address; failed logins lock an email (5 failures, one more attempt every 3 minutes) **before** any password work, and an unknown email behaves identically (no account oracle). At most 4 argon2 operations run at once (`503 busy` beyond that). The extension keeps its queue and waits at least `Retry-After`. nginx adds `limit_req` (20 a second per address, burst 60). Tables in `docs/api.md`. Tests: `tests/test_rate_limit.py` (31), `extension/test/scheduler.test.ts` and `api-client.test.ts`. Residual: see L-12, L-13; the counters live in process memory and reset on restart (ADR-005 accepted this for one process).

**Where.** `server/app/main.py` (no limiter), `server/app/routers/auth.py`, `events.py`, `frames.py`, `extension_tokens.py`, `sessions.py`, `dashboard/nginx.conf` (no `limit_req`). Spec N6: "rate limit on ingest". ADR-005 accepts the limits (60 requests per minute on `/events` and `/frames`, burst 20, 429 with `Retry-After`) and proposes a token bucket in process memory. `docs/api.md` and ADR-004 record that there is no login limit. Nothing was built.

**Evidence.** Probe 6: 200 consecutive requests to `/auth/login` returned the same status each time and never 429. A search of the source for rate limiting, throttling or `Retry-After` finds only documents.

**Impact.**
- Online password guessing and credential stuffing against `/auth/login` has no cost to the attacker except argon2 time. Each attempt costs the server an Argon2id verification at the library's default of 64 MiB and 4 lanes. A few dozen parallel requests saturate a small VPS (CPU and memory), which is also a denial of service by an unauthenticated outsider. The unknown-email path deliberately spends the same time, so it is just as expensive.
- A holder of an extension token (any user) can call `/events`, `/frames`, `/sessions` and `/consent` without limit: unbounded rows, jobs and frame files (see M-10).
- `/auth/register` is guessable only against 256-bit invite codes (not a practical risk) but is still an unauthenticated CPU cost: it hashes the password before the invite is consumed.

**Recommendation.** Implement ADR-005 as accepted (token bucket per extension token on `/events` and `/frames`, 429 with `Retry-After`; the extension already needs to honour it). Add per-IP and per-email throttling and a delay or temporary lock on repeated failed logins, a global cap on concurrent argon2 verifications, and `limit_req` / `limit_conn` in nginx. For IP-based limits the proxy must pass the client address and the API must trust only that proxy (see L-8). Add tests that a burst gets 429 and that a limited client recovers.

### H-2: Request bodies are unbounded except on events and frames

**Status: FIXED (2026-10-09).** `BodyLimitMiddleware` now bounds every route under `/api/` (16 KB default, 800 KB frames, 1 MB hard ceiling on events with the exact 256 KB applied after authentication, so the documented 401, 403, 413 order holds). The 413 is decided by the middleware even when the framework turns a body error into a 400 (this was found while testing: FastAPI answers 400 to a body it cannot read). Validation errors are `{loc, type}` only, so passwords, invite codes and values are never reflected; nginx has `client_max_body_size 1m` and Caddy caps bodies at 1 MB. Re-run of probe 1: the 20 MB login body is now `413` in 0.12 s; probe 2: the 5 MB request's response is 31 bytes and the register 422 no longer contains the password or the invite code. Tests: `tests/test_body_limits.py` (29), which walks the OpenAPI schema so a future route is covered without editing it. Residual: none known for body size.

**Where.** `server/app/main.py:65-68` (the middleware has one rule, for the frame route), `server/app/routers/events.py` (`limited_body`, 256 KB, events only). ADR-013 says "All other endpoints: ≤16 KB" and "Enforce at the ASGI layer before parsing". It is Proposed and was not applied to the other routes.

**Evidence.** Probe 1: a 20 MB JSON body to the unauthenticated `/auth/login` was read and parsed in full (about 0.5 s) and answered with 422 after validation. Probe 2 (second script): the 422 body echoes the submitted value, so a 5 MB request returned 5,000,151 bytes: the response repeats the request. The same 422 body returns the submitted password and invite code (`"input": "short-pw"`), unlike the events route, which strips values on purpose.

**Impact.** Any outsider who can reach the API can make the server buffer and parse arbitrarily large bodies on `/auth/login` and `/auth/register` (memory exhaustion), and get them reflected back. Uvicorn has no body limit by default. In the compose layout the dashboard's nginx (1 MB default `client_max_body_size`) protects only requests that come through it; the extension talks to the API port directly (`extension/src/config.ts`), and the API port is what a hosted deployment would expose. Echoing passwords and invite codes into error responses is also a leak if a proxy or a client logs responses.

**Recommendation.** Apply `BodyLimitMiddleware` to all `/api/` routes with the 16 KB default of ADR-013 and the two existing exceptions; set `client_max_body_size` in nginx; replace FastAPI's default 422 body with one that carries only locations and error types (the events route already shows how); consider uvicorn's `--limit-concurrency`. Add tests for each route.

### H-3: One user can poison the shared Reel cache for every other user

**Status: FIXED (2026-10-09), ADR-024.** The cache is per user: `content_features` and `topic_assignments` start their key with `user_id` (migration `0006`, which copies existing rows to each viewing user so stored snapshots still replay, and drops rows nobody owns). The worker functions take the user; the fingerprint loader, topic evidence, deletion and the prediction topic universe read only that user's rows (the universe was a second path by which one user could change another's predictions: whether `other` existed for the version). Costs: a Reel several users watch is analysed once per user. This is a departure from spec §9 and §12 and ADR-007; ADR-024 is Proposed and the spec was not edited. Tests: `tests/test_cache_isolation.py` (9, including the attack in both orders and the migration on a real downgrade and upgrade), `test_deletion.py`. Residual: a user can still choose the text behind their own rows, which affects only them.

**Where.** `server/app/worker/store.py` (module docstring, `text_input_from_exposure`, `store_features`, `store_assignments`), `server/app/worker/reel_processor.py`, `server/app/routers/events.py:80` (`Shortcode` is a format check only). ADR-007 (Proposed) shares features and assignments across users, keyed by `(reel_shortcode, pipeline_version)`.

**Evidence (code reading; the job consumer does not run yet, see M-8).** The text that is embedded comes from the caption and hashtags in the viewer's own event, which the client wrote. The first event processed for a shortcode writes the cache row (`ON CONFLICT DO NOTHING`; the docstring says "The text of the first viewer's event is the text that is cached"). Later viewers of the same Reel get that user's assignment. Nothing ties the caption to the shortcode, and the shortcode itself is only checked against a pattern, not against Instagram.

**Impact.** Any invited user (a token and consent are enough) can submit an event for the shortcode of a Reel others will watch, with a caption chosen to land in a topic of their choice. If it is processed first, every later user's exposure to that Reel is assigned that topic: their fingerprints, history, predictions and the evaluation of the whole study are shaped by someone else. It also means one user's text-derived data (embedding, OCR text) remains attached to a Reel that another user still has after the first user deletes their data (ADR-007; documented in `privacy.md`, but it is a deletion-semantics gap, not only an integrity one). The attack needs no access beyond what a participant has; it is latent until the consumer is built, then live.

**Recommendation.** Do not let client-supplied text decide a cache row that others read. Options, in order of preference: key the cache by the content that produced it (shortcode plus a hash of the normalised caption, hashtags and OCR text), so different text gets a different row; or compute features per exposure and cache only what is verifiably a function of the shortcode; or require two independent viewers to agree before a row is trusted. Record which user produced a cache row. Add a test where two users send different captions for one shortcode and each gets an assignment from their own text. Decide ADR-007 and ADR-010 with this in mind.

### H-4: No supported hosted deployment: no TLS, insecure cookie default, plaintext token

**Status: FIXED (2026-10-09), with one thing not verified.** `docker-compose.hosted.yml` and `Caddyfile` add TLS (Caddy, HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, 1 MB body cap), close the dashboard's loopback port, publish only 80 and 443, and refuse to start without `REF_DOMAIN`. The API refuses to start with `REF_ENVIRONMENT=production` and `REF_COOKIE_SECURE=false`; the hosted override sets production and secure cookies. The extension's server address is fixed at build time (`VITE_REF_API_BASE`) and the build fails for plain HTTP to any host but loopback, and the manifest asks for host access to that server only (`extension/src/api-base.ts`). CI validates the hosted override and nginx. Tests: `tests/test_hosted_config.py`, `extension/test/api-base.test.ts`. **Not verified:** the stack has not been run against a real domain or certificate (L-14). The default local setup is unchanged: plain HTTP on `127.0.0.1`.

**Where.** `docker-compose.yml` (HTTP only, no Caddy or other TLS service; ADR-019's hosted mode is M7 and not built), `dashboard/nginx.conf` (port 80, no TLS, no `X-Forwarded-*`), `server/app/config.py:50-56` (the session cookie is `Secure` only when `REF_ENVIRONMENT=production`, and compose defaults that variable to `local`), `extension/src/config.ts` (`API_BASE = 'http://127.0.0.1:8000'`, hard-coded) and `extension/manifest.config.ts` (host permissions for localhost only). Spec N6 and `privacy.md` say "TLS whenever the server is not on localhost".

**Evidence.** Probe 4: the default `secure_cookies` is false. Nothing refuses to start in a non-local setting without TLS, and nothing warns.

**Impact.** In local mode this is acceptable (ports are bound to `127.0.0.1`). The moment anyone points a domain or a tunnel at the compose stack, the dashboard session cookie and the extension's Bearer token cross the network in the clear, with `Secure` unset unless the operator knows to change two settings. The extension cannot be configured for another host at all, so the practical risk today is a person following the README to put the dashboard on a server for a cohort. This is a release blocker for hosted use, not a vulnerability of the local setup.

**Recommendation.** Do not describe the stack as deployable until the hosted mode exists: Caddy (or equivalent) terminating TLS with HSTS, forwarded headers handled, the API listening only on the internal network, the extension taking a configured HTTPS base URL through an optional-permission request, and the `Secure` cookie flag on by default (opt out only for `local`). Make the API refuse `REF_ENVIRONMENT=production` without secure cookies. Add the check to CI.

---

## MEDIUM

### M-1: Extension tokens: no list, no expiry, no dashboard UI

`server/app/routers/extension_tokens.py` has `POST` and `DELETE` only. There is no `GET`, so a user cannot see which tokens exist; `DELETE` needs the token id, which is returned once with the token. The dashboard source does not call `/extension-tokens` at all, so tokens are issued by hand with the API. Tokens never expire, record no last-use time, and a user can create any number of them. `ExtensionToken` has no expiry column. Impact: a lost or stolen token can be revoked only by someone who kept its id; the only other remedy is "delete all my data". Recommendation: list endpoint and an Account page section (label, created, last used, revoke), optional expiry (for example 90 days) and a cap on active tokens.

### M-2: Consent can be re-granted by the extension token alone

`server/app/routers/consent.py` (`grant_consent`) accepts `PUT /consent` with a Bearer token, including after the user revoked consent in the dashboard (`DELETE /consent` is allowed from either side). `policy_version` is only format-checked, so any string is accepted as "consent to a policy". A token that is still valid (M-1) lets a script or an old browser profile silently undo a revocation, and `privacy.md` itself notes that consent history is not kept (one row, overwritten), so the revocation leaves no trace. Recommendation: make re-grant after a revocation require an action in the dashboard, or at least keep an append-only consent log (grant, revoke, version, time, via), validate `policy_version` against the versions the server knows, and show the history to the user.

### M-3: No re-authentication for irreversible actions; weak session lifecycle

`DELETE /me/data` (`server/app/routers/me.py`), token issue and the other state changes need only the session cookie and the CSRF header. A stolen cookie (168 hours by default, `session_ttl_hours`) is enough to delete the account and all data irreversibly or to mint a token. There is no idle timeout, no session rotation, no way to end other sessions (logout deletes one), no password change or reset, and login never limits the number of live sessions per user; expired sessions are removed only when the same user logs in again. Recommendation: ask for the password again for delete-all and token issue, add an idle timeout, a "sign out everywhere" action that runs on password change, a cap on sessions per user and a periodic purge of expired rows.

### M-4: No security headers, no CSP; API documentation is public

Probe 2: no `Content-Security-Policy`, `X-Frame-Options` or `frame-ancestors`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security` or `Cache-Control` on any response (neither the API nor `nginx.conf` sets them). Probe 3: `/docs` and `/openapi.json` answer without login. Effects: the dashboard can be framed by another site, so its confirm dialogs (delete-all) are open to clickjacking; there is no second line of defence if an XSS ever appears (none was found: React escapes captions and the evidence link is built on the server from a validated shortcode); authenticated JSON, including captions in the evidence view, may be cached by browsers and proxies; the full API surface is public to outsiders. Recommendation: send a strict CSP (`default-src 'self'`, `frame-ancestors 'none'`), `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on authenticated responses, and turn off `/docs`, `/redoc` and `/openapi.json` outside `local`.

### M-5: Database errors put bound parameters into logs

`server/app/db.py:12-13` creates the engine without `hide_parameters=True`. Probe 5 confirmed that an `IntegrityError` message contains the bound values (the test caption appeared in `str(error)`). An unhandled database error on the ingest path (a dropped connection, a deadlock, a constraint no handler expects) is logged by uvicorn with the exception text, so captions, handles and hashtags can reach the logs. `privacy.md`, `CONTRIBUTING.md` and ADR-012 all state that logs never contain captions, and ADR-012 relies on that to leave logs out of deletion. The property is false on this path. It would be HIGH if logs are shipped off the machine. Recommendation: `create_engine(..., hide_parameters=True)`, a global exception handler that logs the class only, and a test that forces a database error during ingest and scans the log output for the caption.

### M-6: The content script can read the extension token; `privacy.md` says it cannot

`privacy.md` ("Transparency"): the content script "has no storage, no network access and no access to the extension token". In Chrome, `chrome.storage.local` is readable from content scripts by default; `setAccessLevel('TRUSTED_CONTEXTS')` is the switch that stops it, and it is never called (searched `extension/src`). The architecture avoids using the token there, but any bug or compromise in the content script, which runs on every instagram.com page and parses attacker-influenced DOM, could reach the token in plaintext. Related: the host permissions `http://127.0.0.1/*` and `http://localhost/*` cover every port. Recommendation: restrict the token's storage to trusted contexts (`setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' })` where Chrome supports it for the storage area in use; the documentation of that call is clearest for `chrome.storage.session`, so check the behaviour in the target Chrome version, and if it is not available for `local`, keep the token somewhere a content script cannot reach, for example in the service worker's own IndexedDB), narrow the host permissions to the configured port, and add a test that a content-script context cannot read the key. This finding was established by reading the code and the extension documentation, not by running a content script against the storage.

### M-7: The application connects to PostgreSQL as a superuser

`docker-compose.yml` creates the database with `POSTGRES_USER: ref`, which the official image makes a superuser, and the API uses it both for migrations (`alembic upgrade head` in the container command) and for every request. Any SQL injection, SSRF into the database or bug in a query would have full control (file access functions, extensions, other databases). No SQL injection was found (no raw SQL with interpolation in `app/`), but defence in depth is missing. Recommendation: an owner role for migrations and a separate runtime role with only the table privileges the API needs; consider row-level security on the `user_id` tables as a second guard.

### M-8: The job queue is filled but never consumed

`events.py` writes a `Job` for each new event. A search for `SKIP LOCKED`, claiming, retry, backoff and `last_error` finds only the column definitions: no consumer exists (the worker package is a library; `/stats/pipeline` in spec §8 is not built). Consequences: the table grows by one row per event for ever; `jobs.status` has no CHECK constraint (ADR-014 is Proposed) and `payload` is unbounded JSON; a processing failure cannot be seen; raw frames are never deleted "right after feature extraction" (`FrameStore.delete_event` has no caller), so retention is always the full TTL; and fingerprints and predictions exist only when an operator runs `rebuild-fingerprints`. Race conditions of the consumer (two workers, crashed worker holding a job) cannot be reviewed because it does not exist. Recommendation: build the consumer as ADR-006 specifies, with a lease and retry, a CHECK on `status`, a purge of finished jobs, `/stats/pipeline`, and tests with two workers.

### M-9: Frames are validated by magic bytes only

`server/app/frames.py:check_jpeg` accepts a file that starts with `FF D8 FF` and ends with `FF D9`. It is never decoded on upload, and nothing limits its pixel dimensions: a 150 KB JPEG can declare a very large size. Pillow's default bomb guard (`Image.DecompressionBombError`, caught in `visual_embedder.py:126-132`) only triggers above about 178 million pixels, and a decode just below it still needs hundreds of MB. A polyglot (a valid JPEG with other data appended after `FF D9` and zero padding allowed) also passes. The files are not served back to anyone, so the risk is to the worker, not to users. Recommendation: on upload, read only the JPEG header (Pillow `open` without `load`) and refuse dimensions above a limit (for example 2048 px on a side), set `Image.MAX_IMAGE_PIXELS` explicitly in the worker, run decoding in a bounded subprocess, and re-encode what is kept.

### M-10: No per-user quotas

A token holder can create unbounded sessions (`POST /sessions` closes the previous one and makes another), events (50 per request, no cap per session or per day; `observed_at` only has to be inside the session window), jobs, frame files (up to 5 × 150 KB per event, declared by the client in `frame_count`), tokens and consents. This fills the database and the frame volume (which has no size limit and no free-space check) and, combined with H-1, can be done at full speed. Recommendation: per-session and per-day event caps that reflect what a person can watch (the spec's 60 minute session and a plausible Reel rate), a byte quota for the frame volume per user and in total, a refusal to accept uploads below a free-space floor, and a cap on live tokens.

---

## LOW

- **L-1: Cookie hardening.** The cookie is `ref_session` without the `__Host-` prefix, so a sibling subdomain could set it. Combine with H-4 when TLS exists.
- **L-2: Password policy.** Minimum 12 characters, maximum 256, no check against common or breached passwords (`routers/auth.py`). Argon2 uses the library default (Argon2id, 64 MiB, t=3, p=4); the parameters are not configurable or documented.
- **L-3: Small oracles.** `POST /auth/register` answers 409 for an existing email, so anyone holding a valid invite can test whether an email has an account. `POST /events` answers `event_id_conflict` when a random UUID already belongs to another user (UUIDs are unguessable, so the information is negligible).
- **L-4: First write wins on duplicate events.** A replay with the same `event_id` and different content is reported `duplicate` and the new content is discarded silently. This is the right idempotency rule, but a corrupted first upload can never be corrected, and the client is not told the content differed.
- **L-5: Secrets in the environment.** `.env.example` contains `POSTGRES_PASSWORD=change-me` and compose accepts that literal value; the database URL with the password is in the API container's environment (visible to `docker inspect`). No secrets were found in the repository (a scan for private keys and common token formats found nothing; `.env` is ignored).
- **L-6: Retention overshoot.** Frames older than the TTL are removed by a sweeper that runs every 900 seconds by default (`frame_sweep_interval_seconds`), so a frame can live up to 24 hours and 15 minutes, and without the sweeper (interval 0 and no cron) it lives for ever. The spec says at most 24 hours. Nothing alerts if the sweeper stops. Run the sweep more often than the margin you want, count old files in `/stats/pipeline`, and fail the health check when a file is past its TTL.
- **L-7: Logging and audit.** There is no audit trail of security events: failed logins, token issue and revocation, consent changes, deletions are not logged with the user id and time (only deletions produce a count line). There is no request id. Logs are otherwise disciplined (ids, counts and error classes).
- **L-8: Proxy handling.** Uvicorn runs without `--proxy-headers` and nginx forwards only `Host`, so the API never sees the real client address (needed by H-1) and could be tricked by spoofed forwarded headers if added carelessly. Decide the trusted proxy explicitly.
- **L-9: Residual data after deletion.** The shared cache keeps text-derived data of a Reel while another user still has it (ADR-007, disclosed in `privacy.md`). Hosted backups are not rewritten on delete (disclosed; the privacy notice must state the window, M7).
- **L-10: Missing endpoints from the spec.** `GET /sessions/{id}/reels` and `GET /stats/pipeline` (spec §8, N10) do not exist, so there is no queue-depth or failure visibility (see M-8).
- **L-12 (new): a known email can be kept locked.** The login lock (H-1 fix) is per email, so someone who keeps failing a known address keeps it locked at a rate of one real attempt every 3 minutes. At most five invited users, so the trade was accepted; a per-address-and-email key or a CAPTCHA would narrow it. The lock never reveals whether the account exists.
- **L-13 (new): in local mode a client on the machine chooses its own rate-limit address.** nginx and the API trust `X-Forwarded-For` from private networks, and Docker presents connections to the published loopback port from a private gateway address (observed with a stand-in API: a forged `6.6.6.6` arrived as `6.6.6.6, 6.6.6.6`). Anyone who can do that is already on the machine and could call the API directly; the per-user and per-email limits still apply. Hosted mode closes the port.
- **L-14 (new): hosted mode is untested against a real domain.** The files are validated by `docker compose config`, `nginx -t` and tests, and the nginx rules were run in the real image in front of a stand-in API, but no certificate was issued and no browser or extension was connected to a hosted server. Nightly backups and the worker service are still not built.
- **L-11: Supply chain and process.** CI has no dependency audit, secret scan or CodeQL; Actions are pinned by tag, not commit; base images are pinned by tag (`python:3.12-slim`, `node:24-alpine`, `nginx:1.27-alpine`, the pgvector image, `uv:0.12`) and not by digest; Hugging Face models are pinned by full commit SHA (good) but loaded with `from_pretrained` without forcing safetensors. Several security-relevant ADRs are still Proposed (005 mechanism, 007, 012, 013) although code implements parts of them, contrary to `CONTRIBUTING.md`'s rule that a Proposed ADR has no effect until accepted. The CI lint and type checks of `tests/` had drifted (31 type errors remain in eight older test files, none in security code; the rest were fixed with this work). Runtime dependencies were clean on 2026-10-09 (`pip-audit` on the frozen runtime set: no known vulnerabilities; `npm audit --omit=dev` for the dashboard and the extension: 0 vulnerabilities).

---

## Re-audit after the fixes

Done on 2026-10-09 after the four fixes, on the same working tree.

**Probes re-run** (same scripts as the appendix):

| Probe | Before | After |
|---|---|---|
| 1. 20 MB body to `/auth/login` | 422 after parsing it all (0.5 s) | **413** in 0.12 s |
| 2. 5 MB login request, response size | 5,000,151 bytes (the body reflected) | **31 bytes**; register 422 holds no password or invite code |
| 2. Security headers on the API | none | none (M-4 is open; Caddy adds them in hosted mode) |
| 3. `/docs`, `/openapi.json` | 200 | 200 (M-4 is open) |
| 4. Default `secure_cookies` | `False` | `False` for `local` and `ci` by design; `production` is secure and refuses insecure |
| 5. SQLAlchemy error text contains the caption | yes | yes (M-5 is open) |
| 6. 200 rapid logins | one status, no 429 | **429s appear** (burst 10 per address) |

**Checked for new problems the fixes could have introduced:**

- *Does a limit apply to every authenticated route?* The charge is in the three credential dependencies, not in the routes; the class is chosen by the credential that was accepted, not by a header the caller sent (a cookie request with a stray `Authorization` header is still charged as a dashboard request; a test covers it). A route cannot be added without it.
- *Can an attacker turn the limits against others?* An unauthenticated flood from one address is limited by that address and cannot lock another user's account; the email lock is the one place it can (L-12). Memory is bounded (10,000 keys per limiter, least recently used evicted).
- *Is the client address spoofable?* Only from a trusted proxy network, and only to the right-most untrusted entry; a forged left entry, a peer outside the network and a malformed chain change nothing (tests). In local mode the loopback exception is L-13.
- *Does the per-user cache leave another cross-user path?* Every query on the two tables was listed (`grep` of `TopicAssignment` and `ContentFeature` in `server/app`): all filter by `user_id` or join on it, and the only unfiltered ones are the deletion sweeps, which delete rows that no exposure of their own user refers to. The prediction universe was a second path and is fixed.
- *Is the 413 reliable?* It is decided before the body is parsed and survives a framework that answers 400; both are tested with declared and chunked bodies.
- *Do the fixes weaken another control?* The migration keeps replayability (a test replays after upgrade); the 401, 403, 413 order of events is unchanged; no test was loosened to pass (the one that expected a 401 for a grossly oversized unauthenticated events body now expects 401 up to 1 MB and 413 above, with the reason in the test).
- *Dependencies:* no new runtime dependency. `pip-audit` (runtime set) and `npm audit --omit=dev` were not repeated after the changes because none changed.

**Result:** CRITICAL 0, HIGH 0 (was 4), MEDIUM 10 (unchanged), LOW 14 (was 11). The MEDIUM findings were out of scope for this pass and are still open.

## Coverage by audit area

| Area | Result | Notes |
|---|---|---|
| Authentication | Findings M-3, L-2, L-12 (H-1 fixed) | Argon2id; dummy hash on unknown email equalises timing; CSRF header on login and register; no throttling, no lockout, no password change |
| Authorization | Pass | Every router takes `user_id` from `cookie_principal`, `token_principal` or `any_principal`; request models forbid unknown fields, so a body `user_id` is refused |
| IDOR | Pass | Sessions, tokens, events, frames and deletion all filter by `user_id`; another user's object is a 404 (or `existed: false`), indistinguishable from a missing one; evidence, history, predictions and survey are scoped; topic ids are public |
| Session handling | Findings M-3, L-1 | 32-byte random id, only its SHA-256 stored, httpOnly, SameSite=Lax, expiry checked in SQL, logout removes the row; no rotation, idle timeout or sign-out-everywhere |
| Bearer tokens | Findings M-1, M-6, H-4 | 32 random bytes; lookup by hash; revoked tokens fail like unknown ones; a presented bad token never falls back to the cookie; no expiry or list; readable by the content script; sent over plain HTTP in non-local use |
| Token hashing | Pass | SHA-256 of a 256-bit random secret is appropriate (no password-style hashing needed); `hmac.compare_digest` is not required because the comparison is a database lookup on the digest |
| Consent | Finding M-2 | Checked on session start, ingest and frames, under `FOR SHARE` locks; server-side grant time; revocation stops sessions in one transaction; re-grant by token alone; one row, no history |
| Data ownership | Pass | Per-user rows everywhere, the Reel cache included since ADR-024; no structure is shared across users |
| Rate limiting | **H-1 fixed** | Limits per user, per address and per failed-login email; password concurrency cap; `Retry-After` honoured by the extension; L-12, L-13 |
| Payload limits | **H-2 fixed**; M-10 open | Every `/api/` route bounded (16 KB default, events 256 KB after authentication, frames 800 KB, 5 × 150 KB), 413 decided by the middleware; no per-user quotas (M-10) |
| File uploads | Finding M-9 | Names are content hashes inside `<user uuid>/<event uuid>/`, so path traversal is impossible; temp file then `os.replace`; symlinks are never followed; magic bytes only; no pixel cap; no quota |
| MIME validation | Finding M-9 | Declared part type must be `image/jpeg` and magic bytes must match; no decode |
| Deletion | Pass with L-9, M-3 | Idempotent; verification pass over every table and the frame folder before reporting success; account removed last; delete-all needs no re-authentication |
| Retention | Findings L-6, M-8 | Frames capped by TTL and sweeper (24 h 15 min worst case); no consumer deletes after extraction; events and derived data are kept until the user deletes them, as documented |
| Job queue | Finding M-8 | Not consumed |
| Race conditions | Pass | Ingest and frame upload lock the consent and session rows `FOR SHARE`, so a concurrent revocation or stop wins or waits; one active session per user by a partial unique index; invites consumed by an atomic `UPDATE ... RETURNING`; first consent by insert-or-conflict; delete-all locks the user row and re-verifies, and a race with ingest ends in a retryable 500 |
| Duplicate events | Pass with L-4 | `event_id` primary key, `ON CONFLICT DO NOTHING`, job only for a newly stored event, per-event results, cross-user conflicts rejected |
| Database constraints | Mostly pass | CHECKs for sessions, consent, ratings, probabilities and counts; foreign keys with the intended cascades; gaps: no CHECK on `jobs.status` or `reel_exposures.process_status`, `users.email` is unique case-sensitively (the application lowercases), `consents.policy_version` unconstrained, no caps (M-10) |
| Secret handling | Pass with L-5, M-7 | Settings only from the environment, database URL is a `SecretStr`, nothing committed, nothing logged; superuser role (M-7) |
| CORS | Pass (by design) | No CORS middleware: the dashboard and API are same-origin and the extension uses host permissions, so browsers send no cross-origin requests and none can be made with the custom header. ADR-004 records this; revisit if a second origin appears |
| CSRF | Pass | `X-Requested-With: ref-dashboard` required on every cookie-authenticated state change (including login, register, logout and the survey), SameSite=Lax, no CORS to let a foreign page set the header; GET handlers do not change state |
| XSS | Pass with M-4 | No `dangerouslySetInnerHTML`, `innerHTML` or `eval` in the dashboard or extension; React escapes captions; the only link target is built server-side from a validated shortcode and opened with `rel="noopener noreferrer"`; no CSP as a second line (M-4) |
| API abuse | M-10 open | H-1 and H-2 fixed; no per-user quotas on events, sessions, tokens or disk |
| Logging | Finding M-5, L-7 | Application logs carry ids, counts and error classes; the exceptions path does not |
| Privacy leakage | Findings M-5, M-6 | The 422 echo (H-2) and the cross-user cache (H-3, L-9) are fixed; `privacy.md` still claims two things that are not true (M-5, M-6) |

## Controls that were checked and work

- Argon2id with a dummy-hash path for unknown emails; login answers one message for both failures.
- Sessions, tokens and invites are 32 random bytes, shown once, stored as SHA-256.
- `require_csrf` on every cookie-authenticated unsafe method; `X-Requested-With` cannot be set by a foreign page without a CORS preflight that the API never answers.
- No raw SQL with interpolated values; ORM and bound parameters throughout; `np.load` with `allow_pickle=False`.
- Ingest: strict models, NUL refused, shortcode and handle patterns, per-event dwell bounds, session window of ±5 minutes, validation errors that omit values, cross-user `event_id` conflict, idempotent job creation.
- Frames: UUID and hex path components only, content-hash names, temp file and atomic rename, link-safe deletion and sweeping, ownership 404, consent and revoked-session checks under lock.
- Deletion: both operations idempotent, a verification pass that must count zero rows and zero files, jobs and cache sweep included, the account deleted last.
- The container runs as a non-root user; the database is not published; every published port binds to `127.0.0.1`.
- Model revisions are full commit SHAs (a constructor rejects anything else).
- Runtime dependency audits clean on the audit date.

## Not covered

No penetration test, fuzzing, load test or review of the TLS and proxy layer (none exists). The ML worker was read for trust boundaries only (frame decoding, cache keys, logging), not for model-level attacks. Browser-side behaviour of the extension against a hostile Instagram page was reviewed in the message routing and storage only; the adapter's DOM parsing was not fuzzed. The `spikes/` directory was not reviewed. The local `.env` of the operator was not read.

## GitHub issues

The task asked for GitHub issues for every CRITICAL and HIGH finding. **They were not created**: this environment has no GitHub CLI (`gh` is not installed), no GitHub token in the environment, and the repository has no remote and no commits, so there is no repository to file against. Drafts for H-1 to H-4 are in `docs/security-issues/` with the `gh issue create` commands in its `README.md`. All four findings are now fixed, so the drafts should be filed, if at all, as closed issues that link the fix; each draft file carries a "Fixed" note. There are no CRITICAL findings.

## Appendix: probes and commands

The probes were run on 2026-10-09 against the FastAPI application object with a test client and no database (`server/`, with `PYTHONPATH=.`):

1. `POST /api/v1/auth/login` with a 20 MB JSON body and the CSRF header: answered 422 after parsing the whole body (about 0.5 s); no 413.
2. `GET /healthz`: none of CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS, Cache-Control, Permissions-Policy present. `POST /auth/register` with a short password and an invite code: the 422 body returned both values. A 5 MB login request returned a 5,000,151 byte response.
3. `GET /docs` and `GET /openapi.json`: 200 without credentials.
4. `Settings(...).secure_cookies` with defaults: `False`.
5. A SQLAlchemy `IntegrityError` on a table with a text column: the inserted text appeared in `str(error)`.
6. 200 rapid `POST /auth/login` requests: one status code throughout, no 429.

Dependency checks: `uvx pip-audit` on `uv export --frozen --no-dev` (runtime set, no ML group): no known vulnerabilities. `npm audit --omit=dev` in `dashboard/` and `extension/`: 0 vulnerabilities. A repository scan for private keys, AWS, GitHub, Hugging Face and OpenAI-style tokens found nothing.

## Appendix: what the fixes changed, by file

H-1: `server/app/ratelimit.py` (new), `app/auth_deps.py`, `app/routers/auth.py`, `app/main.py`, `app/config.py`, `dashboard/nginx.conf`, `extension/src/api/client.ts`, `extension/src/upload/scheduler.ts`. H-2: `server/app/body_limit.py`, `app/main.py`, `app/routers/events.py`, `dashboard/nginx.conf`. H-3: `server/migrations/versions/0006_cache_per_user.py` (new), `app/models.py`, `app/worker/store.py`, `app/worker/reel_processor.py`, `app/fingerprint/store.py`, `app/fingerprint/evidence.py`, `app/prediction/store.py`, `app/evaluation/collect.py`, `app/deletion.py`. H-4: `docker-compose.hosted.yml` (new), `Caddyfile` (new), `docker-compose.yml`, `app/config.py`, `extension/src/api-base.ts` (new), `extension/src/config.ts`, `extension/manifest.config.ts`, `.github/workflows/ci.yml`, `.env.example`.

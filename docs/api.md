# API (v1)

Source: spec §8. Base path `/api/v1`. FastAPI with Pydantic v2 models; request and response types are shared with the extension and dashboard by contract, not by copy.

## Authentication

| Caller | Credential | Notes |
|---|---|---|
| Dashboard | Cookie `ref_session`: httpOnly, SameSite=Lax, path `/`, Secure in production | State-changing requests must also send `X-Requested-With: ref-dashboard` (else 403) |
| Extension | `Authorization: Bearer <token>` | 32 random bytes (43 URL-safe characters), stored server-side as SHA-256, revocable per token. No CSRF header needed |

- `user_id` comes only from the credential, never from the request body. Request bodies reject unknown fields (422), so a `user_id` in a body is refused.
- Every read, write and delete checks ownership. Another user's row returns 404, the same as a missing row.
- Unauthenticated calls return 401. Bearer failures add `WWW-Authenticate: Bearer`.
- A presented Bearer token is final: an invalid or revoked token never falls back to a cookie.
- Dashboard endpoints (tokens, logout) accept the cookie only; an extension token cannot call them.
- The extension never sends Instagram credentials, cookies or tokens.

Implemented in `server/app/auth_deps.py`: `cookie_principal`, `token_principal`, `any_principal`. Every endpoint that touches user data must depend on one of them and filter by `principal.user_id`.

### Limits and error shapes (security audit H-1, H-2)

**Request size.** Every route under `/api/` has a body limit, decided before the body is parsed (`server/app/body_limit.py`): 16 KB by default, 800 KB for the frame upload, and for `POST /events` a 1 MB hard ceiling with the exact 256 KB applied after authentication (so the documented order stays 401, 403, 413). A larger body is `413 {"detail": "payload_too_large"}`, whether its length was declared or it arrives in chunks; the request is abandoned at the limit. The check is on the application, not on the route, so a route added later is bounded too (a test walks the OpenAPI schema).

**Validation errors** (`422`) carry `{"detail": [{"loc": [...], "type": "..."}]}` and nothing else: never the submitted values (passwords, invite codes, captions) and never a message that could contain them.

**Rate limits** (`server/app/ratelimit.py`, ADR-005 as implemented). A refused request is `429 {"detail": "rate_limited"}` with `Retry-After` in whole seconds; the extension keeps its queue and waits at least that long. Token buckets in process memory (they reset on restart):

| What | Key | Default (per minute, burst) |
|---|---|---|
| `POST /events`, and separately the frame upload | user | 60, 20 (ADR-005) |
| Other extension (Bearer) requests | user | 30, 10 |
| Dashboard (cookie) requests | user | 300, 60 |
| Every `DELETE` | user | 10, 5 |
| Every `/api/` request, before any credential is read | client address | 600, 120 |
| `POST /auth/login` and `/auth/register` | client address | 20, 10 |
| Failed logins | email | 5 failures, then one more attempt every 180 s (checked before any password work; an unknown email behaves the same) |

At most 4 password hashes or checks run at once (64 MiB each); further ones get `503 {"detail": "busy"}` with `Retry-After: 1`. All values are settings (`REF_INGEST_PER_MINUTE` and the others in `.env.example`). The per-user limit does not depend on how many tokens a user holds. The client address is the connection's peer, or, when the peer is in `REF_TRUSTED_PROXIES`, the right-most `X-Forwarded-For` entry that is not itself a trusted proxy; entries a client put to the left are ignored.

### Auth endpoints (implemented)

| Endpoint | Request | Success | Errors |
|---|---|---|---|
| `POST /auth/register` | `{email, password (12+ chars), invite_code}` | 201 `{id, email}` | 403 invalid, used or expired invite; 409 email taken; 403 missing CSRF header; 422 bad input |
| `POST /auth/login` | `{email, password}` | 200 `{id, email}` + session cookie | 401 `Invalid email or password` (same for unknown email); 403 missing CSRF header |
| `POST /auth/logout` | none | 204, cookie cleared, session deleted server-side | 401 not logged in; 403 missing CSRF header |
| `POST /extension-tokens` | `{label?}` (body optional) | 201 `{id, label, created_at, token}`, `Cache-Control: no-store`. **The only time `token` is returned** | 401; 403 missing CSRF header |
| `DELETE /extension-tokens/{id}` | none | 204. Sets `revoked_at`; repeating keeps the first timestamp | 401; 403; 404 unknown or not yours; 422 bad id |

### Consent and session endpoints (implemented)

Extension endpoints, Bearer token only (a dashboard cookie gets 401), unless noted.

| Endpoint | Request | Success | Errors |
|---|---|---|---|
| `PUT /consent` | `{policy_version}` (1-32 characters of `A-Za-z0-9._-`) | 200 `{policy_version, granted_at, revoked_at}`. `granted_at` is the server's clock. Repeating the same grant is a no-op; a new version or a grant after revocation is a fresh grant | 401; 422 (unknown fields such as `granted_at` or `user_id` are refused) |
| `DELETE /consent` | none. Token, or dashboard cookie with the CSRF header | 204. Sets `revoked_at` and stops every active session of the user with `stop_reason=consent_revoked`. Idempotent; fine with no consent on file | 401; 403 cookie without CSRF header |
| `POST /sessions` | none | 201 `{id, started_at, ended_at, status, stop_reason}`. Closes an older active session first (`time_cap` if past its cap, else `browser_restart`) | 401; 403 `consent_required` (absent or revoked); 409 if two starts race |
| `PATCH /sessions/{id}` | `{status: "stopped", stop_reason}`; reason one of `user`, `left_surface`, `tab_closed`, `time_cap`, `browser_restart` | 200 session. Idempotent: an already stopped session is returned unchanged (a server-set `consent_revoked` is kept). Past the 60 minute cap it ends at the cap with `time_cap`. Needs no consent | 401; 404 unknown or not yours; 422 bad status or reason (`consent_revoked` cannot be sent) |

Session vocabulary: `status` is `active` or `stopped`; `stop_reason` is `user`, `left_surface`, `tab_closed`, `time_cap`, `consent_revoked`, `browser_restart` (ADR-014, enforced by database CHECKs). One active session per user. Other users' sessions answer 404, like missing ones.

### Event ingest (implemented)

`POST /events`, Bearer token only. Implemented in `server/app/routers/events.py`; decisions in ADR-022 and ADR-023 (both Proposed).

Request: `{"events": [event, ...]}`, 1 to 50 events, each the spec §8 event payload including `event_id` (UUID version 4) and `session_id`. Two fields are added to the spec payload (ADR-022): `video_duration_status` (`available` or `unavailable`; `video_duration_ms` is null exactly when unavailable) and `caption_truncated` (boolean). Unavailable fields are `{"value": null, "status": "unavailable"}`, and `value` must be null exactly when the status is `unavailable`. `frame_count` is 0 to 5 (0 until M4). Validation is strict: no unknown fields (so no `user_id`), no strings for numbers or booleans, `observed_at` must carry a time zone, no NUL characters.

Checks, in order:

| Check | Answer |
|---|---|
| No valid extension token | 401 |
| No active consent | 403 `consent_required` |
| Body over 256 KB (by `Content-Length`, or counted while streaming) | 413 `payload_too_large` |
| Malformed envelope or any malformed event, more than 50 events | 422 with `[{loc, type}]` only: submitted values are never echoed. Nothing of the batch is stored |
| Otherwise | 200 `{"results": [{event_id, status, reason}]}`, one per event, in request order |

Per-event `status`:

- `stored`: new. A processing job (`jobs.kind = 'exposure'`, payload `{"event_id"}`) is enqueued in the same transaction.
- `duplicate`: this event_id is already stored for the caller. Nothing changes (the first version is kept) and no job is added. Retrying is always safe.
- `rejected`, with `reason`:
  - `session_not_found`: no session with that id belongs to the caller (another user's session answers the same);
  - `session_inactive`: the session is stopped (user, auto-stop, time cap, a newer session, consent revocation);
  - `observed_at_out_of_window`: more than 5 minutes before `started_at`, or more than 5 minutes after the earlier of now and the 60 minute cap;
  - `event_id_conflict`: the event_id belongs to another user's event (which is left untouched).

Consent and the sessions are read `FOR SHARE`, so a revocation or stop that commits concurrently either waits for the batch or is seen by it. The response and the log line hold IDs and counts only. Rate limiting (ADR-005) is built: see "Limits and error shapes" above.

The extension removes an event from its queue only once it is named in `results`. Rejected events are dropped and counted by reason. A whole-batch 400, 413 or 422 is split in half and retried down to single events, so one bad event cannot block the queue. Anything else (network, 401, 404, 429, 5xx, an unreadable reply) keeps the events and backs off.

Endpoints that collect data (events, and frames later) depend on `require_active_consent` in `server/app/consent.py`; it answers 403 `consent_required`.

Invites are created by the operator: `python -m app.manage create-invite [--ttl-hours N]` (prints the code once; only its SHA-256 is stored). Emails are trimmed and lower-cased. Sessions last `REF_SESSION_TTL_HOURS` (default 168).

## Endpoints

| Method | Path | Caller | Purpose |
|---|---|---|---|
| POST | `/auth/register` | dashboard | Create account with an invite code (ADR-004, not in spec §8) |
| POST | `/auth/login` | dashboard | Start session cookie |
| POST | `/auth/logout` | dashboard | End it |
| POST | `/extension-tokens` | dashboard | Issue extension token (returned once) |
| DELETE | `/extension-tokens/{id}` | dashboard | Revoke |
| PUT | `/consent` | extension | Record consent with policy version |
| DELETE | `/consent` | either | Revoke; stops active sessions |
| POST | `/sessions` | extension | Start monitoring session |
| PATCH | `/sessions/{id}` | extension | Stop, with reason |
| POST | `/events` | extension | Batch ≤50 events, idempotent on `event_id` |
| POST | `/events/{event_id}/frames` | extension | Multipart, ≤5 JPEG, ≤150 KB each |
| GET | `/sessions` | dashboard | List sessions |
| GET | `/sessions/{id}/reels` | dashboard | Reels in a session with topics |
| GET | `/fingerprint/current?kind=` | dashboard | `exposure` or `engagement` |
| GET | `/fingerprint/history?kind=&bucket=` | dashboard | Snapshots by `day` or `session` |
| GET | `/topics/{id}/evidence` | dashboard | Contributing Reels and scores |
| GET | `/predictions/next` | dashboard | Distribution plus rolling accuracy per model |
| GET | `/stats/pipeline` | dashboard | Queue depth, failures per stage, latency per stage |
| DELETE | `/sessions/{id}` | dashboard | Delete session and derived data |
| DELETE | `/me/data` | dashboard | Delete everything |
| GET | `/healthz` | ops | Liveness (not under `/api/v1`) |

Dashboard read endpoints serve precomputed snapshots; target p95 <500 ms (N4).

## Event payload

```json
{
  "event_id": "uuid-v4 generated in extension",
  "session_id": "uuid",
  "reel_shortcode": "Cabc123",
  "observed_at": "2026-10-06T18:24:11.120Z",
  "dwell_ms": 8420,
  "video_duration_ms": 15000,
  "creator_handle": {"value": "somecreator", "status": "available"},
  "caption": {"value": "text ...", "status": "available"},
  "hashtags": {"value": ["ai", "coding"], "status": "available"},
  "frame_count": 4,
  "adapter_version": "ig-web-2026.10.1"
}
```

Each captured field carries `status` of `available` or `unavailable`. No field is defaulted silently.

## Ingest rules

Reject when:

- consent is missing or revoked;
- the session is not owned by the caller or not active;
- `observed_at` is outside the session window by more than 5 minutes;
- the body is over 256 KB;
- a frame is not `image/jpeg`.

Ingest is idempotent: the server upserts by `event_id` and enqueues a processing job. Retrying a batch is always safe. Ingest endpoints are rate-limited.

## Next-topic prediction

`GET /api/v1/predictions/next`, dashboard login. The feature's name is **"Predicted next observed topic"** (`label`); it predicts which topic the next Reel the feed shows will have and says nothing about preference (`note`).

Response: `{label, note, available, reason, pipeline_version, as_of, based_on_reels, last_topic, backoff, row_observations, min_row_observations, hybrid_lambda, hybrid, models, evaluation}`. `hybrid` and each of the five `models` (`uniform`, `most_frequent`, `decayed_fingerprint`, `markov`, `hybrid`) have `top` (the five most likely topics with probability) and `rest_probability`; `top` plus `rest_probability` sum to 1. `backoff: true` means the fingerprint stood in for the transition history because the last topic's row had fewer than `min_row_observations` (20) observations. `evaluation`: `scored_reels`, `required_reels` (1500), `complete`, `status`, `models` (per model `all_time` and `rolling`: `n`, `top1`, `top3`, `log_loss`), and `comparisons` and `hybrid_beats_required_baselines`, which are **null until `complete`**. `available: false` with `reason` `no_predictions` or `no_observed_reels` when there is nothing yet. Everything is read from one stored row (`prediction_state`), written when predictions are rebuilt (`python -m app.manage rebuild-fingerprints`).

## Self-report survey

Dashboard login. Evaluation 2 of spec §10: the user rates interest in every topic from 1 to 5 before seeing a fingerprint.

- `GET /api/v1/self-report` returns `{status, completed_at, topics, ratings}`. `status` is `pending`, `submitted` or `skipped`; `topics` are the taxonomy topics to rate (`other` is not one); `ratings` maps topic id (as text) to the rating once submitted.
- `PUT /api/v1/self-report` with `{"ratings": {"<topic id>": 1..5, ...}}`. Needs a rating for every topic and no other (422 otherwise). Ratings are strict integers: `"4"`, `4.0` and `true` are 422. Answered once: the same answer again is 200, a different one is 409 `already_completed`.
- `POST /api/v1/self-report/skip` records a skip (idempotent; 409 after a submission). A skipped user is left out of the evaluation.
- The user comes from the cookie only; a `user_id` in the body is refused. Writes need the CSRF header.
- `DELETE /api/v1/me/data` removes the survey with everything else; `DELETE /api/v1/sessions/{id}` keeps it.

## Deletion

`DELETE /api/v1/sessions/{session_id}` and `DELETE /api/v1/me/data`. Dashboard login plus the `X-Requested-With: ref-dashboard` header (403 without it, 401 with only an extension token). Irreversible.

Both remove the rows, then the files, then run a verification pass that counts what is left in every table and on disk. `200` only when every count is zero: `{"scope": "session"|"all", "existed", "deleted": {table: n}, "remaining": {table: 0, ..., "frame_files": 0}, "frame_files_deleted", "fingerprints_rebuilt", "fingerprints_dropped", "verified": true}`. `500` with `"detail": "deletion_incomplete"` and the same body (`verified: false`, `remaining` showing what is left) when anything is left; calling again finishes the job.

- **Session:** deletes the session, its Reels, their jobs and frames, the user's predictions, prediction state, topic transitions and fingerprint snapshots (cumulative, so stale), then rebuilds the snapshots and the predictions from the Reels that remain. Cache rows (`content_features`, `topic_assignments`) of Reels no remaining exposure of any user refers to are removed. An active session is deleted too. A session that does not exist for the caller (gone, never existed, or someone else's) is `200` with `existed: false`; nothing of anyone else's is touched.
- **Me:** everything of the caller in every table, the whole frame folder, then the account. The login cookie is cleared. The account is deleted only when everything else has verified clean, so a failed run can be repeated by the same login. After a `200` the login is gone: a repeat is `401`.

Revoking consent (`DELETE /consent`) stops active sessions and refuses further events and frames but deletes nothing.

## Topic evidence

`GET /api/v1/topics/{topic_id}/evidence?kind=exposure|engagement&limit=20&offset=0`, dashboard login. Explains why a topic is in the user's **current** fingerprint of that kind. `limit` is 1 to 100, `offset` 0 or more. Items are ordered by contribution, biggest first; equal contributions by `event_id`.

A fingerprint weight is the sum over Reels of `topic score x gain x recency`. Response: `{"topic": {id, slug, label}, "kind", "evidence_available", "reason", "fingerprint": {taken_at, pipeline_version, n_reels, half_life_days, topic_weight, total_weight, topic_share}, "explanation": {gain_rule, formula, contributing_reels, contributions_sum, coverage}, "total", "limit", "offset", "next_offset", "items": [...]}`. Each item: `event_id`, `session_id`, `reel_shortcode`, `reel_url`, `caption` (null if none, cut at 300 characters with `caption_truncated`), `observed_at`, `dwell_ms` and `video_duration_ms` (null if not recorded), `topic_score` (the Reel's fused score for the topic), `p_text`, `p_visual`, `gain`, `recency`, `contribution` (= score x gain x recency), `share_of_topic` (contribution / `topic_weight`). `next_offset` is null on the last page. `coverage` is the sum of all contributions over the stored weight (1.0 when the list explains the whole weight).

`evidence_available: false` with `reason` `no_fingerprint`, `topic_not_in_fingerprint` or `snapshot_without_provenance` (a snapshot saved before the evidence was recorded: rebuild it) and no items. Unknown topic id: 404. Bad `kind`, `limit`, `offset` or a topic id outside 0 to 32767: 422. Only the caller's own Reels are ever read.

## Fingerprint history

`GET /api/v1/fingerprint/history?kind=exposure|engagement&bucket=day|session&limit=90`, dashboard login. `limit` is 1 to 366. Returns the newest `limit` snapshots of the newest pipeline version, **oldest first**: `{"kind", "bucket", "pipeline_version", "topics": [{"topic_id", "slug", "label"}], "points": [{"taken_at", "session_id", "n_reels", "reels_in_bucket", "shares": {"<topic_id>": 0.0-1.0}}]}`. `day` points are one per UTC day with at least one Reel (the state after that day's last Reel); `session` points are one per session. `n_reels` is the running total, `reels_in_bucket` the Reels inside that day or session (null for a snapshot without the figure). Shares of one point sum to 1. No snapshot gives empty `topics` and `points`. The request reads stored snapshots only; its cost depends on `limit`, never on how many Reels were watched. Another user's snapshots are never returned.

## Frame upload

`POST /api/v1/events/{event_id}/frames`, extension token, `multipart/form-data`, one JPEG per file part (the part name is free). Send it after the event is stored. Limits: 5 parts, 150 KB per part, 800 KB per body, and no more distinct frames for an event than its `frame_count` (so never more than 5). Frames are stored under their SHA-256, so a retry stores nothing twice.

Response `200`: `{"event_id", "frames": [{"sha256", "bytes", "status": "stored" | "duplicate"}], "total_frames"}`. Errors carry a code in `detail`, never file content:

| Status | `detail` | Cause |
|---|---|---|
| 401 | | no valid extension token (a dashboard cookie does not count) |
| 403 | `consent_required` | no active consent |
| 404 | `event_not_found` | no such event, or another user's (the two look the same) |
| 409 | `session_revoked` | the event's session was closed by a consent revocation |
| 409 | `frames_expired` | the event is older than the 24 h frame retention |
| 409 | `frame_limit_exceeded` | the event would hold more distinct frames than it declared |
| 413 | `payload_too_large` | body over 800 KB (checked before parsing) |
| 413 | `frame_too_large` | a part over 150 KB |
| 415 | `unsupported_media_type` | not multipart, a part not `image/jpeg`, or bytes that are not a complete JPEG (magic bytes and end marker) |
| 422 | `too_many_frames`, `no_frames`, `unexpected_field` | more than 5 parts, no parts, or a part that is not a file |
| 400 | `malformed_multipart` | the form cannot be parsed |

Frames of a stopped session are accepted (the extension may send them after Stop). Frames are kept only on the local server volume (`REF_FRAME_STORE_DIR`), never in the database, and are deleted after feature extraction or after 24 hours, whichever comes first.

## Contract rules

- Do not add, remove or rename endpoints or payload fields without updating this file and `docs/changelog.md`.
- Breaking changes need a new version path.
- Error bodies and logs contain IDs and error classes only, never captions, OCR text or tokens.

## Open issues

Proposed in `docs/architecture-decisions.md`, not yet accepted:

- Per-endpoint size limits (ADR-013). Only the numbers the owner gave for frames are applied (5 parts, 150 KB each, 800 KB body, with `/events` unchanged at 256 KB); the other endpoints keep no explicit cap.
- Rate-limit mechanism and values (ADR-005). **Login and register are not rate-limited yet**: Argon2id slows guessing but does not stop it.
- Event and frame job ordering and frame readiness (ADR-010).
- There is no `GET /consent` or `GET /sessions` yet. The extension learns the server has no consent from the 403 `consent_required` on session start.
- CORS: not adopted (ADR-004). Same-origin deployment needs none; revisit if the dashboard moves to another origin.
- No endpoint lists extension tokens or reports who is logged in, so the dashboard cannot yet show existing tokens or restore its login state after a reload. Neither is in spec §8.

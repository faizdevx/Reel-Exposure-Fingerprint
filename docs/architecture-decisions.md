# Architecture Decisions: Reel Exposure Fingerprint (MVP)

Source of truth: `docs/final-spec.md`. Companion: `docs/implementation-roadmap.md`.

## Status meanings

- **Accepted (spec)**: stated in the spec. Recorded here for traceability. Changing it changes the spec.
- **Proposed**: fills a gap or resolves a contradiction found in review. Has no effect until the owner accepts it.
- **Pending experiment**: decided by an M0–M6 experiment. The spec names the candidates.

Each record lists context, decision, consequences and, where relevant, the spec section it resolves.

---

## Index

| ADR | Title | Status |
|---|---|---|
| 001 | System topology: MV3 extension + FastAPI + Postgres + one worker | Accepted (spec) |
| 002 | Extension internals: frames and queue in the extension origin | Proposed |
| 003 | Frame capture method and extension permissions | Pending experiment (M0) |
| 004 | Authentication model and invite codes | Accepted (spec) + Accepted (owner, 2026-10-07) except CORS |
| 005 | Ingest limits and rate limiting | Accepted (spec) + Proposed (mechanism) |
| 006 | Job queue in Postgres with `SKIP LOCKED` | Accepted (spec) |
| 007 | Shared feature cache vs per-user deletion | Superseded in practice by 024 (option 2 implemented); status Proposed |
| 008 | Split `pipeline_version` into feature and fingerprint versions | Proposed |
| 009 | Models, topic scoring and late fusion | Accepted (spec) + Pending experiment |
| 010 | Two job kinds and frame readiness | Proposed |
| 011 | Ordered per-user state, fingerprint state table, replay | Proposed |
| 012 | Deletion semantics | Proposed |
| 013 | Per-endpoint payload limits | Proposed |
| 014 | Vocabularies, field status for duration, day timezone | Session vocabularies accepted (owner, 2026-10-07); rest Proposed |
| 015 | Transition and prediction topic definitions | Proposed |
| 016 | Evaluation data: survey before dashboard, hand labels | Proposed |
| 017 | Instagram adapter isolation and self-test | Accepted (spec) |
| 018 | Retention of raw media | Accepted (spec) |
| 019 | Deployment: docker compose, local mode, optional hosted | Accepted (spec) |
| 020 | Schema additions over spec §9: constraints, foreign keys, versioning, timestamps | Accepted (owner, 2026-10-07) |
| 021 | Consent and monitoring sessions | Accepted (owner, 2026-10-07) |
| 022 | Extension shell: content script scope, message routing, upload scheduler | Proposed |
| 023 | Event ingest: per-event results, session rules, delivery order | Proposed |
| 024 | The Reel cache is per user (security audit H-3) | Proposed (implemented on the owner's instruction to fix H-3) |

---

## ADR-001: System topology

**Status:** Accepted (spec §5, §6).

**Decision:**
- Chrome MV3 extension (TypeScript, Vite + CRXJS, `idb`) with content script, service worker and popup.
- FastAPI (Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic).
- PostgreSQL 16 + pgvector for rows, vectors and jobs.
- One worker process with models held in memory.
- Local-volume frame store.
- React SPA dashboard (Vite, TanStack Query, Recharts).

**Consequences:** One machine, one `docker compose up`. No horizontal scaling (N11 accepts this). The worker is a single point of throughput, sized against N3.

---

## ADR-002: Extension internals: frames and queue in the extension origin

**Status:** Proposed. Resolves roadmap G-5.

**Context:** §5 puts the queue in extension-origin IndexedDB. A content script's IndexedDB belongs to `instagram.com`, so the queue must live in the service worker. `chrome.runtime` messages are JSON-serialised and cannot carry `Blob`s.

**Decision:**
- The content script encodes each frame with `OffscreenCanvas.convertToBlob` (JPEG, q70, 512 px short side). It sends the result to the service worker as base64 (≤150 KB per frame, ≤5 per Reel) over a long-lived `chrome.runtime.Port`.
- The service worker owns the IndexedDB queue with two stores: `events` and `frames`, keyed by `event_id`.
- The service worker uploads the event first, then frames. It removes each item from the queue only after a 2xx response.
- Timers use `chrome.alarms`, not `setTimeout`. This covers the 15 s batch, the 60 s off-surface check and the 60 min cap. MV3 service workers suspend when idle.
- Session state lives in `chrome.storage.session`/`local`. Assume the worker can restart at any time.

**Consequences:** Base64 adds ~33% messaging overhead. Upload size is unchanged. If method B (`captureVisibleTab`) is chosen, capture happens in the service worker and the content script sends only the video rect.

---

## ADR-003: Frame capture method and extension permissions

**Status:** Pending experiment (M0, E2). Owner decision D-7.

**Candidates:**
- **A:** `drawImage(video)` into a canvas in the content script. Works only if the canvas is not tainted. Needs no extra permission.
- **B:** `chrome.tabs.captureVisibleTab`, cropped to the video rect. Needs `activeTab` (a user gesture per grant) or `<all_urls>`. Limited to ~2 calls/s. Captures overlays (captions, UI) as well as the video.
- **C:** No frames. Text-only MVP. M4 shrinks to OCR-free (§11).

**Decision rule:** Prefer A. Choose B only if the required permission is acceptable to the owner. `<all_urls>` conflicts with the N7 minimisation goal and with how Chrome Web Store reviewers read the extension. If B needs `<all_urls>` and the owner refuses it, choose C.

**Consequences:** With B, the 0.5/2/4/7/11 s schedule fits the rate limit, but frames include UI overlays, which may change OCR and SigLIP results. Record the chosen method in `adapter_version`.

---

## ADR-004: Authentication model and invite codes

**Status:** Accepted (spec §7) for the model. Invites, `user_sessions` and the CSRF header accepted by the owner on 2026-10-07 and implemented (migration `0002`, `server/app/`). The CORS item is **not adopted**: the dashboard and API are same-origin (nginx in compose, the Vite proxy in dev) and the extension sends a Bearer header, so no cross-origin browser requests exist yet. Revisit when that changes.

**Accepted:**
- Dashboard login uses email + password hashed with argon2id, and an httpOnly, SameSite=Lax session cookie.
- The extension uses a 32-byte random token. The token is shown once, stored as SHA-256 and sent as Bearer. It is revocable per device.
- `user_id` always comes from the credential, never from the request body.
- Local mode binds to `127.0.0.1` with one seeded user.

**Accepted by the owner (2026-10-07), as implemented:**
- `invites(code_sha256 bytea PK, created_at, expires_at, used_at, used_by)`. `created_by` is dropped: the operator CLI that creates invites has no user. `used_by` cascades on user delete, so delete-all leaves no invite row. A CHECK keeps `used_at` and `used_by` set together.
- `python -m app.manage create-invite [--ttl-hours N]` prints a 32-byte code once; only its SHA-256 is stored. `POST /api/v1/auth/register` takes email, password (12+ characters) and the code; it consumes the invite atomically, and a bad invite answers 403 before the email is checked, so it cannot probe for accounts.
- `user_sessions(id_sha256 bytea PK, user_id, created_at, expires_at)`. The cookie holds 32 random bytes; logout deletes the row, so a copied cookie stops working. Default lifetime 168 h (`REF_SESSION_TTL_HOURS`).
- Cookie-authenticated requests that change state must send `X-Requested-With: ref-dashboard`, else 403. Bearer-token requests are exempt.
- Session cookie `ref_session`: httpOnly, SameSite=Lax, path `/`, Secure when `REF_ENVIRONMENT=production` (override with `REF_COOKIE_SECURE`).
- Request bodies reject unknown fields, so a client-supplied `user_id` is refused rather than ignored.

**Not adopted:** CORS allow-list (see Status).

**Consequences:** 2 tables and 1 endpoint beyond §8/§9. Login is rate limited since 2026-10-09 (per address, per failing email, and a cap on concurrent hashing: see ADR-005 "As implemented").

---

## ADR-005: Ingest limits and rate limiting

**Status:** Accepted (spec §8, N6) for the limits. Proposed for the mechanism (G-11). **The mechanism below was implemented on 2026-10-09 as the fix for security audit finding H-1** (`server/app/ratelimit.py`), with the changes listed under "As implemented"; the mechanism stays Proposed until the owner accepts it.

**Decision:** Use a token bucket per extension token, held in API process memory. With a single API process and ≤5 users, nothing more is needed. Starting values: 60 requests/min on `/events` and `/frames`, with a burst of 20. Return 429 with `Retry-After`. The extension's backoff honours `Retry-After`.

**Consequences:** Limits reset on API restart, which is acceptable at this scale. If the API ever runs more than one process, move the counters to Postgres.

**As implemented (2026-10-09, audit H-1):**
- The ADR-005 numbers for ingest hold: 60 a minute, burst 20, `429` with `Retry-After`. They are per **user** and per route kind (`/events`, `/frames`), not per token: a user who mints more tokens gets no more capacity.
- Other classes, charged where the credential is accepted so no route can skip them: extension requests 30 a minute (burst 10), dashboard requests 300 (burst 60), every `DELETE` 10 (burst 5).
- Every `/api/` request is also counted per client address (600 a minute, burst 120), so an unauthenticated flood is limited.
- Login and register: 20 a minute per address (burst 10); failed logins per email: 5, then one more attempt every 3 minutes, checked before any password work (an unknown email is treated the same, so there is no account oracle).
- At most 4 password hashes or checks at once (64 MiB each); more get `503 busy` with `Retry-After`.
- The client address uses `X-Forwarded-For` only when the direct peer is in `REF_TRUSTED_PROXIES`, and then the right-most address that is not itself a trusted proxy.
- nginx limits `/api/` to 20 requests a second per address (burst 60) and 1 MB bodies; the extension honours `Retry-After` and keeps its queue.
- Known limits: a locked-out email can be kept locked by someone who keeps failing it (a 3 minute wait per attempt, acceptable for at most 5 invited users); in local mode anyone who can reach the published port is on the machine and can choose their own address key.

---

## ADR-006: Job queue in Postgres

**Status:** Accepted (spec §6).

**Decision:** Use the `jobs` table with `SELECT … FOR UPDATE SKIP LOCKED`. Retry with exponential backoff via `run_after`. Mark a job failed after N attempts and record `last_error`. No Redis, no Celery.

**Consequences:** `/stats/pipeline` reads queue depth and failures directly from `jobs`. `last_error` must not contain user content (see ADR-012).

---

## ADR-007: Shared feature cache vs per-user deletion

**Status:** Proposed. **Option 2 was implemented on 2026-10-09 as the fix for security audit finding H-3 (ADR-024); the proposed decision below (option 1) is no longer what the code does.** Resolves roadmap C-1.

**Context:** §12 caches features by Reel across users. F13 and the definition of done require "zero rows" after delete-all. `content_features` and `topic_assignments` have no `user_id`.

**Options:**
1. **Reference-counted shared cache:** when a user's data is deleted, also delete cache rows for shortcodes that no remaining `reel_exposures` row references.
2. Per-user cache: add `user_id` to the cache keys. This loses cross-user reuse.
3. Keep the shared cache and define "zero rows" as zero rows *attributable to the user*.

**Proposed decision:** Option 1. After deletion, remove `content_features` and `topic_assignments` rows whose `reel_shortcode` has no remaining exposure from any user. The deletion test checks that no cache row exists for a shortcode only that user saw.

**Rationale:** Option 1 keeps the §12 optimisation and meets the definition of done in single-user mode, where zero rows means literally zero. In cohort mode a shared row remains only while another user still has an exposure to that Reel. It carries no link to the deleting user. OCR text is derived from a public Reel, not from the user.

**Consequences:** Deletion runs one extra anti-join query. With option 1, the cache is never authoritative for "who saw what"; only `reel_exposures` is.

---

## ADR-008: Split `pipeline_version`

**Status:** Proposed. Resolves roadmap C-2. Refines N9 without dropping it.

**Context:** N9 puts models, taxonomy version, fusion weights *and half-life* into `pipeline_version`. Features are keyed by it. Half-life sensitivity runs (§10) would invalidate the feature cache. Visual features cannot be regenerated after frames are deleted (F15).

**Decision:** Use three version strings. Each is a short hash of a canonical JSON config stored in a `pipeline_configs(version, kind, config jsonb)` table.
- `feature_version`: OCR engine + params, text model, visual model, normalisation. Keys `content_features`.
- `assignment_version`: `feature_version` + taxonomy version + τ per modality + fusion weights + `other` threshold. Keys `topic_assignments`.
- `fingerprint_version`: `assignment_version` + half-life + engagement `g` params + prediction params (λ, backoff). Stored on snapshots, state and predictions.

`pipeline_version` in N9 becomes the full triple, recoverable from `fingerprint_version`.

**Consequences:**
- Changing τ, weights or taxonomy recomputes assignments from stored embeddings. No frames are needed, because embeddings are stored.
- Changing half-life replays fingerprints only.
- Changing the **visual model** cannot be applied to past Reels once their frames are gone. Document this as a known limit: a visual model change means old Reels stay on the old `feature_version` or fall back to text-only.

---

## ADR-009: Models, topic scoring and late fusion

**Status:** Accepted (spec §5.2, §6). Parameters pending experiment (E4, E5, E7).

**Accepted:**
- Text model: `intfloat/multilingual-e5-small` (384-d).
- Visual model: SigLIP 2 base, with `google/siglip-base-patch16-256-multilingual` as fallback.
- OCR: RapidOCR, with EasyOCR if Devanagari quality is poor.
- Per-modality score: `p_m(k) = softmax(cos(e_m, t_k^m) / τ_m)`.
- Fusion: `p = Σ w_m p_m`, renormalised over the modalities present. Assign `other` when `max p` is below a threshold.

**Pending:** visual model and dimension (M0 → first migration), OCR engine (M4), `τ_text`, `τ_visual`, `w_text`, `w_visual`, threshold (M4 label audit).

**Proposed details:**
- Apply e5's required prefixes: `query: ` for Reel text and `passage: ` for topic descriptions. Fix this choice once and record it in `feature_version`.
- Each topic's embedding `t_k^m` is the mean of its normalised description embeddings, English and Hindi pooled.
- `other` is a taxonomy entry with no description embeddings. It is assigned only by the threshold.
- Pin Hugging Face revisions (commit SHA) in config.

---

## ADR-010: Two job kinds and frame readiness

**Status:** Proposed. Resolves roadmap C-4 and G-4.

**Decision:**
- `features(reel_shortcode, feature_version)` is enqueued only if no cache row exists. It produces `content_features` and `topic_assignments`.
- `exposure(event_id)` is enqueued for every new event. It applies the exposure to fingerprint state, transitions and predictions once the Reel's assignment exists.
- **Frame readiness:** the event declares `frame_count`. A `features` job runs when the server has received `frame_count` frames, or 10 minutes after the event arrived, whichever comes first. Without frames it runs text-only and records `modalities` accordingly.
- A text-only cache row may be **upgraded** once if frames for the same shortcode arrive later, from this user or another. The `exposure` jobs for affected events are then re-applied through the ordered replay in ADR-011.

**Consequences:** Repeat Reels cost one cheap `exposure` job. The feature cache is no longer frozen at the first viewer's degraded result.

---

## ADR-011: Ordered per-user state, fingerprint state table, replay

**Status:** Proposed. Resolves roadmap C-5, G-1, A-4.

**Context:** The decay update and prequential prediction are order-dependent. Ingest and processing are asynchronous. Snapshots must be reproducible (F10, M5 exit).

**Decision:**
- Add `fingerprint_state(user_id, kind, fingerprint_version, weights jsonb, last_observed_at timestamptz, last_event_id uuid, PK(user_id, kind, fingerprint_version))`.
- `Δt` is computed from `observed_at`, never from processing time.
- `exposure` jobs for one user are applied in `(observed_at, event_id)` order. The worker takes a per-user advisory lock (`pg_advisory_xact_lock`). It applies only events at or before a watermark: all of the user's earlier events must already have assignments.
- If an event arrives with `observed_at` earlier than `last_observed_at`, for example a late upload after an offline period, the worker **replays** that user from the start of the affected session. At ≤100k exposures, a full-user replay is also acceptable.
- Replay is one function: `rebuild(user_id, fingerprint_version)`. The incremental path and the M5 replay script share that code, so "replay reproduces snapshots exactly" holds by construction.
- Predictions for an event are computed from state immediately before that event in this order.

**Consequences:** Late events cost a replay. Replay must be deterministic: fixed ordering, no wall-clock reads, and floats serialised consistently.

---

## ADR-012: Deletion semantics

**Status:** Proposed. Resolves roadmap C-7, G-10.

**Decision:**
- **Delete session:**
  - Delete the session's `reel_exposures`, its frames on disk and its jobs (match on `payload->>'event_id'` / `session_id`).
  - Delete `predictions` whose `made_before_event` belonged to the session.
  - Delete `fingerprint_snapshots` with that `session_id`.
  - Run `rebuild(user_id)` (ADR-011). This regenerates state, transitions, later snapshots and predictions without the session.
  - Run the cache sweep from ADR-007.
- **Delete all data:**
  - Delete the `users` row last, so cascades run.
  - Explicitly delete rows without foreign keys: `predictions`, snapshots, jobs referencing the user, invites used by the user.
  - Delete the user's frame directory and run the cache sweep.
  - Invalidate cookies and tokens.
  - Return counts per table. A test asserts zero counts and an empty directory.
- Add FKs where §9 lacks them: `fingerprint_snapshots.session_id → sessions ON DELETE CASCADE` and `predictions.made_before_event → reel_exposures ON DELETE CASCADE`. **This bullet is accepted and implemented via ADR-020. The rest of ADR-012 is still Proposed.**
- Logs and `jobs.last_error` contain only IDs and error classes, never captions, OCR text or tokens. This means log retention does not have to be part of deletion.
- If hosted backups exist, the privacy notice states the backup expiry window. Delete-all does not rewrite backups.

**Consequences:** Session delete is O(user history) because of the rebuild, which is fine at N11 scale. "Delete all my data" means the user account goes too. If the owner wants the account kept, delete everything except `users` and `extension_tokens`. Owner to choose; the proposal is to delete the account.

---

## ADR-013: Per-endpoint payload limits

**Status:** Proposed. Resolves roadmap C-6. **Implemented on 2026-10-09 as the fix for security audit finding H-2:** `BodyLimitMiddleware` bounds every route under `/api/` (16 KB unless a rule gives more: frames 800 KB, events 1 MB hard ceiling with the exact 256 KB applied after authentication), the 413 is decided by the middleware even if the framework turns a body error into a 400, nginx has `client_max_body_size 1m`, and validation errors no longer echo submitted values. Stays Proposed until the owner accepts it.

**Decision:**
- `POST /events`: JSON body ≤256 KB, ≤50 events.
- `POST /events/{id}/frames`: body ≤800 KB, ≤5 parts. Each part is `image/jpeg`, verified by magic bytes and not only by the header, and is ≤150 KB.
- All other endpoints: ≤16 KB.
- Enforce at the ASGI layer before parsing. Caddy also enforces a global cap when hosted.

---

## ADR-014: Vocabularies, duration status, day timezone

**Status:** The first two items (session vocabularies) are **accepted by the owner (2026-10-07)** and enforced by migration `0003`. The rest is still Proposed. Resolves roadmap G-6, G-7, G-9, A-5, A-7.

**Decision:**
- `sessions.status` ∈ {`active`, `stopped`}. *(accepted)*
- `stop_reason` ∈ {`user`, `left_surface`, `tab_closed`, `time_cap`, `consent_revoked`, `browser_restart`}. *(accepted; `consent_revoked` is set by the server only)*
- `video_duration_ms` gets a field status like the other fields. If it is unavailable, the engagement weight uses `max(duration, 5 s)` with duration = 5 s, and the event is flagged.
- `observed_at` is when the Reel became primary.
- Scrolling back to a Reel creates a new exposure with a new `event_id`.
- Daily snapshots bucket by the user's IANA timezone. Store it on `users.timezone`, default from the browser at consent time. The daily job runs after local midnight per user.

---

## ADR-015: Transition and prediction topic definitions

**Status:** Proposed. Resolves roadmap G-8.

**Decision:**
- The "observed topic" of an exposure is `argmax_k p_fused(k)`, including `other`.
- Transitions count observed topic → next observed topic **within a session only**. A new session does not form a transition from the previous session's last Reel.
- `actual_topic` in `predictions` uses the same definition.
- Log loss is computed against `actual_topic`, with probabilities floored at 1e-6.
- Baselines:
  - **uniform**
  - **most-frequent:** the user's most frequent observed topic so far
  - **decayed fingerprint:** exposure fingerprint normalised
  - **Markov:** add-one smoothed, backoff to fingerprint when the row has <20 observations
  - **hybrid:** `λ`-mix

**Consequences:** Argmax discards soft information. Report this as a limitation. Soft-target scoring can be added later without schema change.

---

## ADR-016: Evaluation data: survey before dashboard, hand labels

**Status:** Proposed. Resolves roadmap C-3, G-2.

**Decision:**
- Add `interest_surveys(user_id, topic_id, rating smallint 1–5, taxonomy_version, answered_at)`.
- The dashboard redirects to the survey until it is completed. The survey ships in **M3**, with the first dashboard, not in M6. The correlation script stays in M6.
- Add `label_audit(reel_shortcode, labeller_user_id, topic_id, taxonomy_version, labelled_at)` and a minimal labelling page in the dashboard. It shows caption, hashtags and a link; frames are not shown, because they are deleted.
- Labels are taken from the 200-Reel sample. Selection is stratified across users and days, recorded as a stored seed.
- If the developer has already viewed the dashboard before the survey exists, the developer's correlation result is reported as contaminated.

**Consequences:** Adds 2 tables. Moves one item from M6 to M3, which changes milestone content but not overall scope.

---

## ADR-017: Instagram adapter isolation and self-test

**Status:** Accepted (spec N8, §7).

**Decision:**
- All selectors and DOM heuristics live in `extension/src/ig-adapter.ts`.
- The adapter exposes `selfTest()`, which returns which selectors fail. The popup shows "adapter out of date" when it fails.
- Prefer structure, ARIA and `href` patterns over class names.
- Saved HTML fixtures run in Vitest.
- `adapter_version` is sent with each event.
- The extension never reads IG cookies, storage, tokens or network responses.

**Proposed addition:** Code review checklist item and a lint rule. The rule bans `document.cookie`, `fetch`/XHR interception, `chrome.cookies` and `chrome.webRequest` in the extension.

---

## ADR-018: Retention of raw media

**Status:** Accepted (spec F15, §12).

**Decision:** The worker deletes frames right after feature extraction. An hourly sweeper deletes any frame older than 24 h. Raw media is never written to Postgres. Only embeddings and OCR text are stored.

---

## ADR-019: Deployment

**Status:** Accepted (spec §6, §7, M7). **Hosted mode (TLS) was built on 2026-10-09 as the fix for security audit finding H-4:** `docker-compose.hosted.yml` and `Caddyfile` (see `docs/deployment.md`). Nightly `pg_dump` and the worker service are still not built.

**Decision:**
- Run with `docker compose`: `db`, `api`, `worker` and a static `dashboard` server, plus `caddy` in hosted mode.
- Local mode binds to `127.0.0.1` and creates one seeded user.
- Hosted mode targets a 4 vCPU / 8 GB VPS with Caddy TLS and nightly `pg_dump` (retention per ADR-012).
- Self-only vs cohort (owner decision D-1) determines whether hosted mode is built at all.

---

## ADR-020: Schema additions over spec §9

**Status:** Accepted by the owner, 2026-10-07. Implemented in migration `0001` (edited in place: it had never been applied to a persistent database or committed).

**Context:** Spec §9 defines tables, columns, cascades and three indexes, but no CHECK constraints, leaves several references without foreign keys, and gives `predictions` no pipeline version (N9 covers only assignments and snapshots; `CONTRIBUTING.md` requires a version on every pipeline result).

**Decision:**
- **CHECK constraints:**
  - `fingerprint_snapshots.kind IN ('exposure', 'engagement')`, `bucket IN ('session', 'day')` (values written in §9).
  - `>= 0`: `reel_exposures.dwell_ms`, `reel_exposures.video_duration_ms`, `fingerprint_snapshots.n_reels`, `topic_transitions.n`, `jobs.attempts`.
  - In `[0, 1]`: `content_features.ocr_confidence`, `topic_assignments.p_text`, `p_visual`, `p_fused`.
  - NULL passes every check, so optional columns stay optional.
- **Foreign keys:**
  - `fingerprint_snapshots.session_id → sessions ON DELETE CASCADE` (ADR-012).
  - `predictions.made_before_event → reel_exposures.event_id ON DELETE CASCADE` (ADR-012).
  - `topic_transitions.from_topic`, `to_topic` and `predictions.actual_topic → topics` (NO ACTION: a referenced topic cannot be deleted).
- **Columns:** `predictions.pipeline_version text NOT NULL`; `created_at timestamptz NOT NULL DEFAULT now()` on `predictions` and `jobs`.

**Not included (rejected or out of scope):**
- `reel_exposures.received_at`, which was not approved.
- Status vocabularies for `sessions.status`, `stop_reason`, `reel_exposures.process_status` and `jobs.status`, because the spec does not define them (ADR-014, Proposed).
- Server-side UUID defaults, because the application supplies IDs.
- Any ADR-007, 008, 011 or 016 tables or columns.

**Consequences:**
- Session delete now removes that session's snapshots and the predictions made before its events. Aggregates in `topic_transitions` still need the ADR-012 rebuild.
- A `topics` row must exist before any transition or prediction references it, including `other`.
- Spec §9 text is unchanged; this ADR records the deviation. Update §9 if the owner wants the spec to match.
- `tests/test_database.py` asserts the exact sets of foreign keys (with delete rules) and CHECK constraint names, because Alembic autogenerate does not compare CHECK constraints.


---

## ADR-021: Consent and monitoring sessions

**Status:** Accepted by the owner, 2026-10-07. Implemented in migration `0003`, `server/app/routers/consent.py`, `sessions.py`, and the extension's `consent/`, `session/`, `events/` modules.

**Decision:**

*Consent*
- One `consents` row per user (spec §9). `PUT /api/v1/consent {policy_version}` records it with the **server's** clock as `granted_at`. A client cannot send a grant time. Repeating the same grant changes nothing; a new version, or a grant after a revocation, is a fresh grant that clears `revoked_at`. The previous revocation time is not kept (one row).
- `policy_version` is chosen by whoever ships the consent text (the extension). The server checks only the format (`[A-Za-z0-9._-]`, 1-32 characters) and stores it.
- `DELETE /api/v1/consent` (extension token, or dashboard cookie with the CSRF header) sets `revoked_at` and, in the same transaction, stops every active session of the user with reason `consent_revoked`. It is idempotent.
- Extension endpoints that collect data must depend on `require_active_consent`; `POST /sessions` does. Without active consent they answer 403 `consent_required`.
- The extension keeps a local consent record, written only after the server confirmed the grant, and valid only for the current policy version. A different version means no consent.
- Revoking is local first: consent cleared, session ended, unsent queue **purged**, then the server is told. If the server cannot be reached the revocation is remembered and retried (alarm, popup open, browser start). A new grant cancels a pending revocation.
- The consent text lives in `extension/src/consent/policy.ts` as data. A test pins its hash to `POLICY_VERSION`, so wording cannot change without a new version.

*Sessions*
- At most one active session per user, enforced by a partial unique index. `POST /sessions` first closes an older active session: `time_cap` (ended at its cap) if overdue, else `browser_restart`.
- `PATCH /sessions/{id}` takes `{status: "stopped", stop_reason}`. A client may send every reason except `consent_revoked`. It is idempotent: stopping a stopped session returns it unchanged. It needs no consent: stopping is always safe.
- The 60 minute cap is also enforced by the server: a stop after the cap ends the session at `started_at + 60 min` with reason `time_cap`.
- Extension rules: start needs consent, a token and the active tab on the Reels surface. The cap and the 60 second off-surface rule use `chrome.alarms` and persisted state, with inclusive thresholds (`>=`). Closing the monitored tab, or a browser restart, also ends the session. A tab URL the browser hides counts as "left", because only instagram.com URLs are visible. Stopping is local first; a stop the server could not receive is retried.
- Only `/reels/*` counts as the surface (spec F3). `/reel/<code>/` does not, pending experiment A-2.

*The zero-events guarantee (F1)*
- `emitEvent` (`extension/src/events/emitter.ts`) is the only way an event reaches the upload queue. It writes nothing unless consent is active **and** a session is running that no automatic rule has ended. Tests assert that no other file calls `enqueueEvent`, and that only the API client touches the network.

*Permissions*
- The manifest gains `alarms` and host permissions for `https://www.instagram.com/*` (to see the tab URL), `http://127.0.0.1/*` and `http://localhost/*` (the REF server in local mode; no CORS needed). CORS stays unadopted (ADR-004). A hosted server will need an optional-permission request (M7).

**Consequences:** Chrome warns about access to instagram.com at install. The extension talks only to a local server until M7. Server-side deletion of uploaded data is still missing (M5), and the consent text says so. `ended_at` for an overdue session is the cap, not the time anyone noticed.


---

## ADR-022: Extension shell: content script scope, message routing, upload scheduler

**Status:** Proposed. The code below is implemented because the owner asked for the extension shell, but the decisions are not accepted yet. The first one departs from the literal wording of the request, so the owner should confirm it.

**Decision:**

1. **Content script injection.** The manifest injects the content script on `https://www.instagram.com/*`, top frame only, not on `/reels/*` alone. Instagram navigates without reloading the page, so a script matched only to `/reels/*` would never load for a user who arrives from the home feed. The script is idle everywhere except the Reels surface: it checks its own address (`isReelsSurface`) and sends no message and touches no DOM elsewhere. The service worker separately activates only the monitored tab, only on the surface. If the owner prefers a literal `/reels/*` match, change `matches` in `manifest.config.ts`; the cost is that the marker will not appear after an in-app navigation until the tab is reloaded.
2. **The content script owns no storage.** It has no queue, no token and no network access. It learns "monitoring on or off" from the service worker (push on every session or tab change, plus one `contentHello` question at load) and, from M2, reports Reels through a `reelEvent` message. The service worker validates the sender (the tab and URL come from the browser, not the message), adds `session_id` itself, and writes through `emitEvent`.
3. **Two message vocabularies, split by sender.** A sender with a tab (a content script) may send only `contentHello` and `reelEvent`. The popup (no tab) may send only the existing popup requests. Messages from another extension, or of the wrong kind, get no answer. `routeMessage` in `background/handlers.ts` is the single entry point.
4. **Upload scheduler.** The service worker uploads queued events in batches of at most 50 through `ApiClient.postEvents`. *Amended with event ingest (ADR-023):* a batch is also capped at 240 KB of JSON. Queued events are sent 15 s after the first is queued (spec §5.1), or at once when ten are waiting. Chrome fires alarms no sooner than 30 s, so the 15 s wait uses an in-memory timer, with a persisted due time and an alarm as the backstop if the worker is suspended. Failures back off 30 s, 1, 2, 4 ... up to 15 min; while a backoff is pending, new events do not trigger extra requests. The server dedupes by `event_id` (idempotent ingest). A 403 `consent_required` clears local consent and deletes the queue. Uploading continues after a session ends while consent holds; it never runs without consent.
5. **Request envelope.** `POST /events` is sent as `{"events": [ ... ]}`, each item the event body plus `event_id`. Spec §8 gives the single-event payload and "batch ≤50" but no envelope. The server endpoint is M2; the envelope must be confirmed then.

6. **Exposure event (added with Reel exposure tracking).** One `reelEvent` per viewing instance, sent by the content script when the instance ends (primary changes, pagehide, tracking stops). The body is the spec §8 payload plus two fields the spec lacks: `video_duration_status` (so a missing duration is `unavailable`, not a number) and `caption_truncated` (so a cut-off caption is not presented as complete). `frame_count` is 0 until frame capture (M4). `session_id` is added by the service worker. A finished viewing is accepted from the monitored tab even after it left the Reels surface; consent and the session are still required. Limits that follow: the last, still-open viewing is lost when the session ends by Stop, the 60 minute cap or consent revocation, because the service worker has already closed the session; and a viewing is lost if the browser dies before pagehide while the tab is hidden.

**Consequences:** The `/events` endpoint now exists (ADR-023), which also settles when the extension drops an event: only when the server rejects it for good. Nothing in the extension has run on real Instagram yet: the `reelEvent` path is tested with synthetic messages and fixtures.

---

## ADR-023: Event ingest: per-event results, session rules, delivery order

**Status:** Proposed. Implemented because the owner asked for reliable event ingest (`server/app/routers/events.py`, `extension/src/upload/scheduler.ts`, `extension/src/session/controller.ts`), but not accepted yet. Builds on ADR-022 items 4 and 5, which it needs accepted.

**Context:** Spec §8 says to reject an event when consent is missing, the session is not owned or not active, `observed_at` is outside the session window by more than 5 minutes, or the body is over 256 KB, and to be idempotent on `event_id`. It does not say what a batch answers when only some events are bad, and "reject when the session is not active" conflicts with "no event loss" (F8, N5): events still queued when a session stops, for example during an outage, would be refused when they finally arrive.

**Decision:**

1. **Per-event results.** Request-level failures are HTTP errors: 401, 403 `consent_required`, 413, and 422 for any malformed event (nothing of the batch is stored). Otherwise 200 with one result per event: `stored`, `duplicate` or `rejected` with a reason (`session_not_found`, `session_inactive`, `observed_at_out_of_window`, `event_id_conflict`). Another user's session answers `session_not_found`, like a missing one.
2. **Idempotency.** `INSERT ... ON CONFLICT (event_id) DO NOTHING`. A replay keeps the first version and adds no job. An event_id already used by another user is `rejected: event_id_conflict` and the other row is not touched.
3. **Jobs.** One `jobs` row per newly stored event, `kind = 'exposure'`, `payload = {"event_id"}`, in the ingest transaction (the owner's request; matches the `exposure` job of ADR-010, which stays Proposed). No job per shortcode yet.
4. **Session rule.** An event is accepted only while its session's status is `active`. The window is `[started_at - 5 min, min(now, started_at + 60 min) + 5 min]`, so a session that was never stopped (crashed browser) still takes its own events after the cap, but nothing observed later.
5. **Delivery order on the extension.** Because stopped sessions refuse events, the extension uploads the queue **before** it tells the server a session stopped. If the upload fails (offline), the stop is kept pending with the events and both are retried, events first. This applies to Stop, the auto-stop rules, tab close and browser restart. Starting a new session also first sends pending events and stops. Consequence: while offline, the server's session stays `active` past the user's stop, and `ended_at` is the time the stop finally arrived.
6. **Dropping.** The extension removes an event from its queue only when the server names it in `results`. Rejected events are dropped and counted by reason in `chrome.storage.local` (`uploadDropped`, counts only). A batch refused as a whole with 400, 413 or 422 is split in half until the bad event is found; that single event is dropped and counted. Network errors, 401, 404, 429, 5xx and unreadable replies keep everything.
7. **Fields without a column.** `caption_truncated` and `frame_count` go into `reel_exposures.field_status` next to the per-field statuses, to avoid a schema change. A migration can move them later.
8. **Races.** Consent and the batch's sessions are read `FOR SHARE` in the ingest transaction, so a concurrent revocation or stop either waits or is seen.
9. **pagehide.** The content script marks the event it sends from `pagehide` with `flush: true`; the service worker uploads at once instead of waiting 15 s.

**Consequences:** An outage that outlasts the 60 minute cap still delivers the session's events, because the server session is only stopped when the extension says so (or a new session starts, which the extension delays until the queue is sent). Two known gaps remain: the Reel on screen at Stop is not recorded (ADR-022 item 6), and a session closed by `POST /sessions` from another device would refuse this device's late events. Rate limiting (ADR-005) was added on 2026-10-09; the extension keeps its queue and waits at least `Retry-After` on a 429.

---

## ADR-024: The Reel cache is per user (security audit H-3)

**Status:** Proposed. Implemented on 2026-10-09 because the owner asked for all HIGH security fixes; the approach is the engineer's and the owner may choose another. It changes spec §9 (the keys of `content_features` and `topic_assignments`) and the intent of §12 ("analyse a Reel once"), so `docs/final-spec.md` needs a dedicated edit if this is accepted. The spec was not edited.

**Context:** Features and topic assignments were cached by `(reel_shortcode, pipeline_version)` and shared by all users, first writer wins. The text that is embedded (caption, hashtags) and the frames are whatever the viewer's client sent, and the shortcode is not verified. So one invited user could submit an event for the shortcode of a Reel others will watch, with a caption of their choice, and decide the topic distribution of everyone's later exposure to that Reel: their fingerprints, history, predictions and the evaluation. It also left one user's text-derived data on a Reel that another user still had after the first deleted everything (ADR-007).

**Options:**
1. **Per-user cache (chosen).** Both tables start their key with `user_id` and cascade on user delete. Nothing one user sends can influence another user's rows.
2. Key the shared cache by a hash of the content that produced it. Two users with the same text would share a row, but frames (the visual modality) differ byte for byte per client, so sharing would rarely happen, while the lookup and the key grow complicated.
3. Keep the shared cache and require agreement between viewers. Needs several viewers per Reel; useless at five users.

**Decision:** Option 1. Migration `0006` adds `user_id`, copies every existing row to each user who has an exposure of that Reel (so stored snapshots still replay exactly) and drops rows nobody owns. `load_observations`, topic evidence, deletion and the prediction universe read only the user's own rows; the worker functions take the user. The set of topics a user's predictions are made over is now computed from that user's own rows too (a shared "other is used" test would have let another user change it).

**Consequences:**
- The same Reel is analysed once per user, not once overall. At five users and the throughput of N3 this costs at most five times the embedding work for Reels several users watch.
- Deletion gets simpler and stronger: a user's cache rows are removed with their exposures or with the account, and no other user's rows are read or kept for them.
- Downgrading the migration would restore the shared cache (and the weakness).
- Not addressed: a user can still choose the text behind their *own* rows. That is their own data and affects only them.

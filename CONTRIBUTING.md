# CONTRIBUTING.md: Reel Exposure Fingerprint

## Project purpose

Research MVP. A Chrome extension passively observes the Instagram Reels a consenting user watches during manually started sessions. A self-hosted backend assigns each Reel a topic distribution and maintains two time-decayed fingerprints per user:

- **exposure**: what the feed showed
- **engagement**: what the user dwelled on

A dashboard shows both fingerprints, how they change over time, per-topic evidence, and next-topic prediction against baselines. Evaluation (spec §10) is what makes this research, not a demo.

Source documents:

- `docs/final-spec.md`: **source of truth**. Never change it silently.
- `docs/implementation-roadmap.md`: requirements map, milestones M0–M7, risks, spec gaps (C-/G-/A- ids).
- `docs/architecture-decisions.md`: ADR-001 to ADR-023. **Proposed** ADRs have no effect until the owner accepts them. If a Proposed ADR is needed to proceed, stop and ask the owner.

## Architecture

```
Chrome MV3 extension
  content script (ig-adapter: detect Reel, metadata, dwell, frames)
  service worker (session state, IndexedDB queue, upload)
  popup (consent, token, start/stop, status)
        | HTTPS + Bearer token, batched events + frames
FastAPI (auth, ingest, read APIs)  ->  PostgreSQL 16 + pgvector (data + jobs table)
                                   ->  frame store (local volume, 24 h TTL)
Worker (1 process): OCR, e5 text embedding, SigLIP image embedding
                    -> taxonomy scoring + late fusion -> fingerprints -> prediction
React dashboard  -- session cookie -->  FastAPI
```

Spec §5 has the full design and §9 the database schema.

## Stack

- **Extension:** Manifest V3, TypeScript, Vite + CRXJS, `idb`.
- **Server:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic.
- **Database and queue:** PostgreSQL 16 + pgvector. The job queue is the `jobs` table with `FOR UPDATE SKIP LOCKED`. No Redis, no Celery.
- **Models:**
  - `intfloat/multilingual-e5-small`
  - SigLIP 2 base (fallback `google/siglip-base-patch16-256-multilingual`)
  - RapidOCR (EasyOCR if Devanagari is poor)
  - Final choices are made in M0/M4.
- **Dashboard:** Vite, React, TypeScript, TanStack Query, Recharts.
- **Deploy:** docker compose; Caddy for TLS when hosted.
- **Tests:** pytest, Vitest, saved HTML fixtures.

## Directory structure

```
extension/          MV3 extension (Vitest tests in extension/test)
  src/ig-adapter.ts   ALL Instagram selectors and DOM heuristics (only here)
server/             FastAPI app (app/), Alembic migrations (migrations/), worker and scripts later
dashboard/          React SPA (Vitest tests in dashboard/test)
tests/              pytest tests for the server (run from server/)
taxonomy/           versioned topic taxonomy files (EN + HI descriptions)
spikes/             M0 throwaway code; never imported by shipped code
docs/               spec, roadmap, ADRs, milestone reports
.github/workflows/  CI
docker-compose.yml
```

Do not create new top-level directories without updating this file.

## Coding rules

- Follow the current milestone in `docs/implementation-roadmap.md` §9. Do not build later-milestone or out-of-MVP features (roadmap §3.2).
- **Typing:**
  - Python: strict typing, checked by `ruff` and `mypy`.
  - TypeScript: `strict` mode, checked by `eslint` and `tsc`.
  - Message and API payloads are typed on both sides.
- Keep the stack in spec §6. Any new dependency needs a reason in the PR or commit body.
- Load configuration from environment variables through one settings module per app. Never hardcode configuration.
- No silent defaults:
  - Every captured field carries status `available` or `unavailable`.
  - Missing data is never filled with a fake value.
- Every modality (text, OCR, visual) may fail independently. A failure in one must not block the others.
- Apply exposures for a user in `(observed_at, event_id)` order (ADR-011). Compute `Δt` from `observed_at`, never from wall-clock time.
- The MV3 service worker can be suspended at any time:
  - Use `chrome.alarms`, not long `setTimeout`.
  - Persist state in `chrome.storage` or IndexedDB.
- Keep content-script work off the scroll path: <5 ms per mutation batch, frame encoding via `OffscreenCanvas`.

## Testing rules

- Every endpoint needs these tests:
  - 401 when unauthenticated.
  - 404 when another user's row is accessed.
  - The happy path.
- Ingest tests:
  - Replaying the same `event_id` creates no duplicate.
  - Payload-limit rejection.
  - Consent-missing rejection.
  - Rejection when the session is not active or not owned.
- Adapter tests run offline against saved HTML fixtures in Vitest. Scrub fixtures of real handles, captions and personal data before committing.
- Deletion tests assert zero rows in **every** table for the user, an empty frame directory, and the cache sweep (ADR-007).
- Reproducibility test: replay from stored assignments must reproduce stored snapshots exactly.
- Delivery test: network drop and browser restart lose no events and create no duplicates.
- Milestone exit checks in the roadmap are acceptance tests. A milestone is not done until they pass.
- Report test results as they are. Never mark failing or skipped checks as passing.

## Security rules

- **Instagram boundary:** the extension reads only rendered DOM and video pixels. It must **never** do any of the following:
  - read Instagram credentials, passwords or tokens;
  - read cookies (`document.cookie`, `chrome.cookies`);
  - read Instagram network responses, or intercept fetch/XHR (`chrome.webRequest`);
  - call private or undocumented Instagram APIs.
- **Authorisation:**
  - `user_id` comes only from the Bearer token or session cookie, never from the request body.
  - The server checks ownership on every row it reads, writes or deletes.
- **Auth:**
  - Passwords are hashed with argon2id.
  - The dashboard uses an httpOnly SameSite=Lax cookie plus a CSRF header check.
  - Extension tokens are 32 random bytes, shown once, stored as SHA-256, and revocable per device.
- **Secrets:**
  - Secrets live only in environment variables: DB password, session secret, invite seeds, TLS config.
  - Never commit secrets, tokens or `.env` files. Commit `.env.example` with placeholders.
  - Never log secrets.
- **Input:**
  - Enforce per-endpoint size limits (ADR-013).
  - Accept JPEG only, verified by magic bytes.
  - Batch ≤50 events.
  - `observed_at` must be within the session window ±5 min.
  - Rate-limit ingest (ADR-005).
- **Transport:** TLS whenever not on localhost. Local mode binds to `127.0.0.1`.
- **Ingest idempotency:** upsert by `event_id`; retries must be safe.

## Privacy rules

- **Consent:**
  - Nothing is collected or sent without active consent, checked in the extension **and** on the server.
  - Revoking consent stops active sessions and uploads at once.
  - Consent records the policy version.
- Collect only on the Reels surface. The content script is idle elsewhere.
- A visible badge and an in-page marker are always shown while collecting.
- **No automation:** no auto-scroll, auto-play, scripted navigation or simulated input. The user browses; the extension only observes.
- **Raw media retention ≤24 hours:**
  - Delete frames right after feature extraction, with a sweeper as a 24 h backstop.
  - Never store raw media in Postgres.
- **Deletion:** session delete and delete-all must remove DB rows **and** files, including jobs, predictions, snapshots and orphaned cache rows (ADR-012).
- **Minimisation:** store embeddings and text, not media. Logs and `jobs.last_error` contain IDs and error classes only. They never contain captions, OCR text or tokens.
- Per-user isolation everywhere, including dashboard queries and evidence views.

## Reproducibility rules

- Every pipeline result (features, assignments, snapshots, state, predictions) stores its version: `pipeline_version` per N9, split per ADR-008 once that ADR is accepted.
- The version is a hash of a canonical config: models with pinned Hugging Face revisions, taxonomy version, τ, fusion weights, threshold, half-life, λ.
- Model inference must be deterministic where possible:
  - eval mode;
  - fixed seeds;
  - fixed preprocessing;
  - no sampling;
  - fixed thread settings when they affect results.
- Record known nondeterminism.
- Changing a model, taxonomy or parameter means a new version, never a silent overwrite.
- Evaluation scripts record inputs, seed, version and outputs. Report results whichever way they fall.

## Git rules

- `main` stays runnable. Work on short-lived branches named `m<N>/<topic>` (e.g. `m2/ingest-queue`).
- Use Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`). One logical change per commit.
- Never commit secrets, `.env`, real user data, raw frames, model weights or unscrubbed IG fixtures.
- Schema changes go only through Alembic migrations. Never edit an applied migration.
- Commit or push only when asked.

## Documentation rules

- Spec changes are explicit: edit `docs/final-spec.md` in a dedicated commit that states what changed and why.
- New or changed design decisions get an ADR in `docs/architecture-decisions.md`, with status Proposed, Accepted or Rejected.
- Each milestone ends with a short report in `docs/` (e.g. `docs/m0-feasibility.md`) that records its exit-check results.
- Reference the docs instead of copying them. Keep this file short.

## Important constraints

- Respect MVP scope (spec §2, roadmap §3). Spec section 1 (the "1.3" out-of-scope list) is missing; ask the owner before assuming anything in it.
- M0 is go/no-go: unreliable Reel detection means stop. If no frame method works, the MVP becomes text-only.
- The M4 label audit (≥70% top-1 fused) gates M5 history and M6 prediction.
- Sized for ≤5 users and ≤100k exposures. Do not over-engineer: no vector index, no extra services.
- **Owner decisions are open** (roadmap §10: D-1 to D-7). Do not decide them in code.
- Verify model IDs and licences on Hugging Face at build time.

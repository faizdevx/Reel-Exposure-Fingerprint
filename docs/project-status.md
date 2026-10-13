# Project Status

Last updated: 2026-10-07. Update this file at the end of every milestone and whenever an owner decision is made.

## Current state

- **Phase:** implementation started. The M1 skeleton exists and passes all checks. M0 is **not finished**.
- **M0:** the spike tooling is built (`spikes/`). The CPU benchmark for e5, SigLIP and RapidOCR has run on synthetic frames, but its results are not yet reviewed or recorded. The browser experiments (Reel detection, frame capture, metadata, dwell) have **not been run**: they need a manual session on a live Instagram account. `docs/m0-feasibility-report.md` does not exist yet, so there is no GO / NO-GO decision.
- **M1 was started on the owner's explicit request, before M0 exit.** The skeleton does not depend on the browser results, but it carries one provisional value: `visual_embedding vector(768)` (spec §9 says to set the width after M0).
- **Spec:** `docs/final-spec.md` is the source of truth. Spec section 1 (the "1.3" out-of-scope list) is missing from the file.

## Milestones

Durations from spec §11 (15 h/week). Status values: `not started`, `in progress`, `done`, `blocked`.

| Milestone | Scope | Duration | Status |
|---|---|---|---|
| M0 | Feasibility spikes, go/no-go | 4–5 days | in progress: tooling built, browser experiments and report pending |
| M1 | Skeleton and auth | 1 week | skeleton, auth, consent and monitoring sessions done (server and extension); dashboard login and token screens not started |
| M2 | Observation and reliable ingest | 1.5 weeks | in progress: adapter, exposure tracking, queue, upload and `POST /events` built and tested offline; exit checks in Chrome not run |
| M3 | Text pipeline, first fingerprint, first dashboard | 1.5 weeks | not started |
| M4 | Frames, OCR, visual embedding, fusion | 2 weeks | not started |
| M5 | Temporal view, evidence, deletion | 1 week | not started |
| M6 | Prediction and evaluation harness | 1.5 weeks | not started |
| M7 | Hardening and MVP release | 1 week | not started |

Gates:

- M0 is go/no-go. Unreliable Reel detection means stop and rethink.
- If no frame-capture method works in M0, the MVP becomes text-only.
- M4 exit requires fused top-1 ≥70% on the label set (or a documented reason and taxonomy revision) before M5 history and M6 prediction start.
- M6 claims need ≥1,500 Reels per user, so collect data daily from the end of M2.

## M1 skeleton: what exists

| Part | State |
|---|---|
| `server/` | FastAPI app with `GET /healthz` (liveness, no database dependency). Settings from `REF_*` environment variables. SQLAlchemy 2 models and engine. Alembic migration `0001` creates the spec §9 schema and the pgvector extension |
| `extension/` | MV3 manifest (permissions `storage`, `alarms`; hosts instagram.com, 127.0.0.1, localhost; one content script), service worker, popup, `idb` event queue keyed by `event_id` |
| `dashboard/` | React shell with a server status check, TanStack Query, and an empty-state Recharts fingerprint chart |
| `docker-compose.yml` | `db` (pgvector/pgvector:pg16), `api` (runs migrations, then serves), `dashboard` (nginx). Localhost-only ports. Smoke-tested on 2026-10-07 |
| CI | `.github/workflows/ci.yml`: server, extension, dashboard, compose validation |
| Auth | Invite-only registration, login and logout (Argon2id, httpOnly SameSite=Lax cookie, server-side sessions, CSRF header), extension tokens (issue once, SHA-256 at rest, revoke, Bearer auth), identity only from the credential. Popup stores the token locally |
| Consent and sessions | `PUT`/`DELETE /consent`, `POST`/`PATCH /sessions`. Extension: consent screen, Start/Stop, 60 minute cap, 60 second off-surface rule, tab close, browser restart; `emitEvent` gate: zero events without consent or a live session. One active session per user, DB-enforced |
| Extension shell | Popup (consent, token entry, Start/Stop, status, monitoring badge), service worker (session state, alarm-based upload scheduler, IndexedDB queue), content script (idle off-surface, in-page marker). ADR-022 Proposed |
| Instagram adapter | `ig-adapter.ts`: primary Reel (IntersectionObserver, >=60%), shortcode, handle, caption, hashtags, duration, playing, tab visibility, self-test. Tested only against synthetic fixtures; never run on real Instagram; not wired into the content script |
| Exposure tracking | `exposure/tracker.ts`: one event per viewing instance with dwell (primary, visible, playing), pagehide handling, duplicate prevention; events go content script to service worker queue. Tested only with fake observers and synthetic fixtures; never run in Chrome on Instagram |
| Event ingest | `POST /api/v1/events`: per-event `stored`/`duplicate`/`rejected`, idempotent by `event_id`, one `exposure` job per new event, consent, session and window checks, 256 KB limit. Extension uploads every 15 s or 10 events, backs off, sends queued events before a stop, flushes on pagehide, resumes after restart. ADR-023 Proposed |
| Dashboard | Pages `/login`, `/`, `/sessions`: both fingerprints as normalised shares (total always 100.0%), session list, loading/empty/error states, responsive. Reads `GET /sessions` and `GET /fingerprint/current` (precomputed snapshots). Playwright suite against a mocked API; one manual real-stack pass. Not built: token screens, history chart, evidence drawer (M5), anything shown with real worker output (the worker does not write snapshots yet) |
| Prediction | Five models, prequential storage, `GET /predictions/next`, dashboard panel, evaluation with a 1,500 Reel minimum (`docs/prediction.md`). Built and tested; **nothing is claimed**: the evaluation needs 1,500 scored Reels per user and then an interval-based comparison with the baselines. Rebuilt on demand (`rebuild-fingerprints`); no job loop yet |
| Security | Audit `docs/security-audit.md`: **0 CRITICAL, 0 HIGH** after the 2026-10-09 fixes (rate limiting, request size limits, per-user Reel cache, hosted TLS mode with an HTTPS-only extension build), 10 MEDIUM and 14 LOW open. Not ready for a cohort until at least M-1 (tokens), M-2 (consent re-grant), M-3 (re-authentication) and M-5 (parameters in logs) are closed. Hosted mode is built but untested against a real domain; GitHub issues could not be created (no `gh`, token or remote) |
| Fingerprint replay | `python -m app.manage replay-fingerprint` rebuilds fingerprints and snapshots from stored assignments, read-only, and compares with the stored ones within `rel 1e-9` / `abs 1e-12` (`docs/replay.md`). Exit 0/1/2. Parameters come from stored provenance |
| Evaluation harness | `python -m app.evaluation report` runs the four spec §10 evaluations (labelling, self-report Spearman, prediction, stability) and writes `docs/evaluation-report.md`. Self-report survey built (API, dashboard, deletion). **No result exists:** there is no labelled set, no ratings and no real user, so the report says "not run" in every section. A synthetic dry run is in `docs/evaluation-dry-run.md` and is not a finding |
| Data deletion | `DELETE /sessions/{id}` and `DELETE /me/data`, with derived data, fingerprint rebuild, shared-cache sweep, frame removal and a verification pass that must find zero rows and zero files before it answers 200. Dashboard: per-session delete and the Account page. ADR-007 and ADR-012 are implemented but still Proposed |
| Topic evidence | `GET /topics/{id}/evidence` and the dashboard panel: the Reels behind a topic with link, caption, time, dwell, scores, gain, recency, contribution; paged; contributions reconcile with the stored weight. Needs snapshots built after provenance was added (older ones answer `snapshot_without_provenance`; rebuild them) |
| M5 temporal views | Daily and session snapshots (`rebuild`, `rebuild_user`, `python -m app.manage rebuild-fingerprints`), `GET /fingerprint/history`, dashboard `/history` (share over time, exposure vs engagement, daily snapshots, session timeline). Reads snapshots only. Nothing runs the rebuild automatically yet (no job loop); days are UTC. Still open in M5: deletion endpoints and the evidence drawer |
| Fusion and evaluation | Late fusion, `other` assignment (threshold unset) and the evaluation scripts `python -m app.evaluation` are built and tested. Nothing is tuned: it needs the hand-labelled set (200 Reels, from real frames), which does not exist (`docs/fusion-evaluation.md`) |
| Visual embedding | `app/worker/` visual stages, fusion and `process_reel`, tested with fake and real SigLIP. Model not chosen (`REF_VISUAL_MODEL` has no default; both candidates registered and benchmarked on synthetic frames only: `docs/visual-evaluation.md`). Not in the job loop: no worker feeds frames to it yet |
| OCR | Benchmark harness and dry run on synthetic frames only (`docs/ocr-evaluation.md`). No engine chosen, no OCR stage in the worker. Blocked on 50 real frames from the M0 probe |
| Frame upload | `POST /events/{event_id}/frames` (server side only): JPEG validation, limits, consent and ownership checks, local-volume store, 24 h TTL sweeper. The extension does not capture or send frames yet: that waits for the M0 probe result (`docs/m0-feasibility-report.md` does not exist) |
| Fingerprint engine | `server/app/fingerprint/`: exposure and engagement topic-weight vectors with 2^(-dt/h) decay (h = 7 days), `rebuild` replay, session snapshots with `fingerprint_version`. Pure maths tested by hand-computed cases; snapshot read/write tested against pgvector. Not wired to the worker: no job loop, no state table (ADR-011 Proposed), no daily snapshots |
| Taxonomy v1 | `taxonomy/topics.json` (28 topics, English and Hindi descriptions), validated by `server/app/taxonomy.py`; hand-written and provisional (D-4 open), no Hindi review yet. Nothing consumes it yet: no embedding, scoring or `topics` table seed |
| Not built (as of this row; later rows say what exists) | Dashboard token screens, the worker, frame capture |

Checks on 2026-10-07, all passing: server ruff, mypy strict, 247 pytest tests (234 need a pgvector database); extension eslint, tsc, 346 Vitest tests, build; dashboard eslint, tsc, 6 Vitest tests, build. The CI workflow itself has not run yet because nothing is committed.

## Definition of done (spec §2)

A consenting user runs 3+ sessions on separate days totalling 500+ Reels. The dashboard shows both fingerprints, their change over time, evidence per topic, and a prediction panel with accuracy against baselines. "Delete all my data" leaves zero rows and zero files.

## Open owner decisions

| ID | Decision | Needed by |
|---|---|---|
| D-1 | Self-only or cohort (sets ethics/legal work and whether hosted mode exists) | M0 |
| D-2 | Deadline (spec says 10 weeks at 15 h/week is the honest figure) | M0 |
| D-3 | Research claim: exposure, engagement, or the gap between them | Before M3 |
| D-4 | Taxonomy source: hand-written or adapted | Start of M3 |
| D-5 | Supply the missing spec section 1 | M0 |
| D-6 | Accept or reject each Proposed ADR | End of M0 |
| D-7 | Acceptable extension permissions if frame method B needs `<all_urls>` | End of M0 |

## Known spec issues

Contradictions and gaps found in review are listed in `docs/implementation-roadmap.md` §6, with proposed resolutions in `docs/architecture-decisions.md` (status Proposed). The schema is spec §9 plus the owner-approved additions in ADR-020, the ADR-004 auth tables, and the ADR-014 session vocabularies with the one-active-session rule (ADR-021). The rest of ADR-014 and the other Proposed ADRs are not applied.

## Top risks

1. Frame capture from Instagram's `<video>` may be blocked (tainted canvas).
2. Reel detection or selectors may be unreliable or break when Instagram changes its page.
3. Zero-shot topic accuracy may fall below 70% top-1.
4. CPU throughput may miss N3 (≥1 Reel per 3 s on 4 vCPU, no GPU).
5. MV3 service worker suspension may drop timers.
6. Data may not accumulate in time for prediction claims.

Full list: roadmap §5.

## Next actions

Manual setup and the by-hand checks for the current phase: `docs/manual-setup.md`.

1. Owner runs the browser experiments following `spikes/README.md`, then shares the exported log summary.
2. ML bench results are reviewed in `docs/m0-ml-bench-summary.md` (draft, synthetic frames only). Still to do: benchmark EasyOCR for Hindi, pick a SigLIP model on real frames, then write `docs/m0-feasibility-report.md` with a GO / NO-GO per risky component.
3. Owner answers D-1, D-2, D-5, D-6, D-7.
4. After M0 exit: finish M1 (dashboard login and token screens), then M2. The extension permissions changed (`alarms`, instagram.com and localhost hosts): load it in Chrome and check the consent flow by hand, which has not been done.
5. Decide whether to add the two small endpoints the dashboard needs (list tokens, current login state): neither is in spec §8. Rate-limit login and register (ADR-005).

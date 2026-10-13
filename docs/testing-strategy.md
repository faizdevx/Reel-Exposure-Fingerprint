# Testing Strategy

Sources: spec §3 (acceptance checks), §4, §10, §11 and the testing rules in `CONTRIBUTING.md`. Report results as they are. Never mark a failing or skipped check as passing.

## Tooling

| Layer | Tool | Scope |
|---|---|---|
| Server | pytest | API, ingest, worker stages, deletion, replay |
| Extension and dashboard | Vitest | Adapter, queue, popup logic, dashboard components |
| Static checks | ruff + mypy (Python); eslint + tsc (TypeScript) | Run in CI |
| Fixtures | Saved HTML of the Reels page | Offline adapter tests |

CI (from M1) runs lint, type-check, pytest and Vitest on every change.

## Test levels

### Unit

- Adapter parsing against HTML fixtures: shortcode, handle, caption, hashtags, field status.
- Scoring math: softmax per modality, late fusion with missing-modality renormalisation, `other` threshold.
- Fingerprint update `W ← W·2^(−Δt/h) + g·p` and the engagement weight `g`, including the sub-1.5 s skip case.
- Perceptual-hash dedupe of frames.

### Integration

- Every endpoint: 401 unauthenticated, 404 for another user's row, happy path.
- Ingest: same `event_id` replayed creates no duplicate; oversized body rejected; non-JPEG frame rejected; consent missing rejected; session not active or not owned rejected; `observed_at` outside the window rejected.
- Worker: `SKIP LOCKED` claim, retry with backoff, independent modality failure (a Reel with no caption still gets a topic from frames).
- Deletion: zero rows in every table for the user, empty frame directory, no orphaned cache rows.
- Retention: sweeper removes frames older than 24 h; frames are deleted after feature extraction.

### End to end (manual plus scripted)

- Scroll Reels by hand on a live account; compare detected exposures with the real count.
- Start, stop and auto-stop paths; verify session `stop_reason`.
- Network drop and browser restart mid-session.

## Acceptance checks per requirement

| Req | Check |
|---|---|
| F1 | With consent absent or revoked, the extension emits zero events (network log). Automated in `extension/test/gate.test.ts`: consent absent, malformed, for another policy version, or revoked (also mid-session, also with the server unreachable) gives zero queued events and zero `fetch` calls; revoking purges the queue. Source scan: only `emitter.ts` writes events, only `api/client.ts` uses the network |
| F2 | `session-controller.test.ts` (start rules, 60 minute cap, 59 s vs 60 s off-surface, return before the countdown ends, tab close, browser restart, retry of unreported stops) and `tests/test_consent_sessions.py` (server side: cap, one active session, idempotent stop, cross-user 404) |
| F3 | Content script idle on all paths other than the Reels surface. Automated in `extension/test/content-script.test.ts` (no message, no DOM change on seven non-Reels URLs, including look-alike hosts and `/reel/`), `routing.test.ts` (service worker refuses other tabs and URLs) and `manifest.test.ts`. Still to do by hand: Chrome check on a real instagram.com page |
| F4 | Scroll 50 Reels by hand: ≥95% detected, 0 duplicates. Primary rule (59% no, 60% yes, area, hand-over, removal) automated in `extension/test/ig-adapter.test.ts` with a fake `IntersectionObserver`; real-page detection rate not measured yet |
| F5 | No field silently defaulted. Automated for the adapter: every field is `available` or `unavailable` with a reason (`ig-adapter.test.ts`). Real-page availability rates not measured yet |
| F6 | Dwell stops when tab hidden or video paused. Automated in `extension/test/exposure.test.ts` (pause, hidden tab, buffering, end of video, hidden at pagehide, wall-clock jump, switching Reel). Not yet checked against real playback events in Chrome |
| F7 | Median upload <250 KB per Reel |
| F4 (events) | One exposure per viewing instance, no duplicates: `exposure.test.ts` covers repeated observer callbacks, leave then pagehide, pagehide then stop, a refresh after the end, and the same event twice at the service worker (one queued row). Real detection rate (>=95% of 50 hand-scrolled Reels) not measured |
| F8 / N5 | Kill network mid-session, restore: no loss, no duplicates; browser restart loses nothing. Automated: `extension/test/delivery.test.ts` runs the real extension code (service worker handlers, IndexedDB queue, scheduler, sessions, browser restart) against a fake server with the ingest rules, through a 2 minute outage: mid-session, with lost replies, Stop pressed offline, restart during and after the outage, pagehide flush, a new session after the outage. Every viewing is stored exactly once with one job. `tests/test_events.py` replays overlapping batches against the real endpoint and database, and sends identical batches concurrently. `extension/test/scheduler.test.ts` covers per-event results, splitting refused batches, byte budget, backoff, the 15 s timer. Not yet: a real outage in Chrome (manual step in `docs/manual-setup.md`) |
| F9 | Reel with no caption still gets a topic from frames |
| F10 | Snapshots reproducible from stored assignments |
| F11 | Click topic → list of Reels with link, caption, score |
| F13 | Count query returns zero rows; files gone |
| F14 | Indicator always on while collecting. Automated: popup badge view, marker follows pushes (`content-script.test.ts`, `render.test.ts`, `content-state.test.ts`). Toolbar badge and in-page marker not yet seen in Chrome |
| F15 | Frame directory empty the next day |
| N1 | Detection <500 ms from Reel becoming primary |
| N2 | No dropped scroll frames; <5 ms per mutation batch (Chrome Performance trace) |
| N3 | ≥1 Reel per 3 s on 4 vCPU; 200-Reel session within 10 min |
| N4 | Dashboard API p95 <500 ms |
| N6 | Tests per item: hashed tokens, ownership, size limits, rate limit, no IG credentials or cookies |
| N8 | Adapter self-test reports which selectors fail. `runSelfTest` is tested against a matching page, a changed page, a missing primary, and for containing no page text. Surfacing it in the popup is M7 |
| N9 | Every assignment and snapshot stores its pipeline version |

## Reproducibility tests

- Replay from stored assignments must reproduce stored snapshots exactly.
- Model inference runs in eval mode with fixed seeds and preprocessing. Record any known nondeterminism.

## Research evaluation (spec §10)

| Question | Method | Pass bar |
|---|---|---|
| Are topic labels correct? | Hand-label 200 Reels across users; compare to top-1 fused topic; also report text-only and visual-only | ≥70% top-1, ≥85% top-3. Below that, fix the taxonomy before building downstream |
| Does the fingerprint reflect the user? | User rates interest 1–5 per topic before the dashboard; Spearman correlation against each fingerprint | Report both. Expect engagement > exposure; if not, that is a finding |
| Does prediction work? | Prequential: predict before each Reel, then update. Top-1, top-3, log loss | Hybrid must beat most-frequent-topic and decayed-fingerprint baselines. If not, say so |
| Is the fingerprint stable? | Replay events in shuffled-session order and half-life 3/7/14 days | Top-5 topic rank should not flip on small parameter changes |

Minimum data before any claim: 500 Reels per user for fingerprints, 1,500+ for transition-based prediction. Evaluation scripts record inputs, seed, version and outputs.

## Fixtures and test data

- Scrub fixtures of real handles, captions and personal data before committing.
- Never commit real user data, raw frames or model weights.
- Re-capture fixtures when the adapter self-test reports failing selectors.
- The current adapter fixtures (`extension/test/fixtures/`) are synthetic, written to match the adapter's own assumptions. They prove the code does what it says, not that Instagram still looks like that. Replace them with scrubbed captures after the M0 browser run.

## Gates

- A milestone is done only when its exit checks in `docs/implementation-roadmap.md` pass. Record results in the milestone report in `docs/`.
- The M4 label audit gates M5 history and M6 prediction.

## Open issue

The survey-before-dashboard requirement (§10) conflicts with its scheduling in M6. The proposed fix is ADR-016 (status Proposed).

## Security fixes (audit H-1 to H-4)

- **H-1, `test_rate_limit.py`:** the limiter (burst, refill with a fake clock, retry time, independent keys, bounded memory, concurrent callers never exceed the burst, the ADR-005 default values), the client address (an unconfigured server ignores `X-Forwarded-For`; behind a trusted proxy the right-most untrusted entry wins; a forged left entry, a peer outside the trusted network and a malformed chain change nothing), the password cap (503 with `Retry-After`), and against the real application: failed logins lock one email before any password work and a success clears it, an unknown email behaves identically, the address limits on login, register and every `/api/` request, ingest 429 after the burst and recovery as the bucket refills, a refused ingest stores nothing, frames and events have separate buckets, one user's limit does not touch another's and extra tokens buy nothing, deletions are limited harder. Extension: `Retry-After` is parsed and honoured, never shortened, capped, and the queue is kept (`api-client.test.ts`, `scheduler.test.ts`).
- **H-2, `test_body_limits.py`:** the middleware (declared length, chunked streaming, an application that swallows the error and answers 400, rule precedence, paths outside `/api/`), every body-carrying route discovered from the OpenAPI schema refuses an oversized body with 413 before it runs, events keep the 401, 403, 413 order, a 5 MB unauthenticated login is refused and not repeated, and validation errors never contain the submitted password, invite code or value.
- **H-3, `test_cache_isolation.py`:** a user who is processed first with a hostile caption does not decide another user's Reel (both orders), each user has their own rows, the fingerprint loader and the prediction universe never read another user's rows, the keys start with the user and cascade, a row without a user is refused, and migration `0006` copies shared rows to each viewer and drops unowned ones (run against a real downgrade and upgrade). `test_deletion.py` checks that one user's deletion never touches another user's cache rows.
- **H-4, `test_hosted_config.py`:** production cookies are secure and `REF_COOKIE_SECURE=false` is refused in production (settings and environment forms); the login cookie carries `Secure`, `HttpOnly`, `SameSite=Lax` in production and no `Secure` in local; the base compose publishes loopback only; the hosted override sets production, closes the dashboard port, publishes only 80 and 443, pins Caddy, requires `REF_DOMAIN` (`docker compose config` is run when Docker is present); the Caddyfile has HSTS and a body cap; nginx limits bodies and rates and trusts only private networks for the client address. Extension: `api-base.test.ts` (HTTPS unless loopback, no credentials or path, the host permissions of default and hosted builds, the manifest for a hosted build, a build for an insecure remote server fails).

Manual checks that are not in the suite: the nginx rules were run in the real `nginx:1.27-alpine` image in front of a stand-in API (a 2 MB body is 413 and an 800 KB body passes; 300 parallel requests produced 94 `429`s; a forged `X-Forwarded-For` sent through the published loopback port reached the API as `6.6.6.6, 6.6.6.6`, because Docker presents that connection from a private gateway address that nginx trusts, so in local mode a client on the machine can choose its own address key. Hosted mode closes that port; this is the limit stated in `docs/deployment.md`).

## Fingerprint replay

`tests/test_replay_compare.py` (pure) and `tests/test_fingerprint_replay.py` (pgvector) implement the CONTRIBUTING.md rule "replay from stored assignments must reproduce stored snapshots exactly". They check that a fresh rebuild replays with zero difference, that the parameters come from the stored provenance (a half-life of 3 days replays under its own version, not the default), that the replay never writes, that the result does not depend on input order or on the process hash seed, and that each kind of change is found and named: a weight beyond tolerance, `n_reels`, `taken_at`, a changed assignment, a changed dwell, an added or deleted Reel, a missing or extra stored row. Tolerance edges are tested directly (inside, outside, near zero, large values, NaN, infinity). `--from/--to` are tested to limit the comparison and not the replay. Unreplayable states give exit 2 with a reason.

## Evaluation harness

`tests/test_evaluation_research.py` (pure) checks the statistics against hand-computed values (average ranks with ties; Spearman including `8 / sqrt(95)` for a tied example; Kendall's tau-b; "undefined" for a constant side), the session shuffle (every Reel kept, order and spacing inside a session kept, the total span kept, sessions stay contiguous, reproducible from a seed, one session cannot be shuffled), a hand-built top-5 flip (30 Reels of topic 1, then 8 of topic 2 seven days later: baseline [1, 2], half-life 3 days gives [2, 1]), and the rules that keep the report honest: no labelled set means no figure, a fused top-1 of 69% fails the 70% bar (no rounding), 60 labelled Reels are never judged, a figure that could not be computed is missing and not a failed bar, pure noise is not reported as a win for the hybrid, a report with NaN is still strict JSON, and the Markdown never contains success wording and prints a miss as a miss. `tests/test_self_report.py` (pgvector) covers the survey API (401, CSRF, every topic rated, strict 1 to 5 integers, answered once, skip, other users' answers, a `user_id` in the body refused), deletion (delete-all removes it, a session delete keeps it), and the report built from the database (reads survey and Reels, changes nothing, same data and seed give the same report, the command writes Markdown and JSON). Dashboard: Vitest and Playwright cover the survey gate (fingerprints are not even requested while it is pending, history is guarded, save needs every topic, CSRF header and body, skip, error keeps the answers, not asked again).

## Prediction

`tests/test_prediction_engine.py` checks the maths by hand: add-one smoothing, each model, the backoff boundary (19 observations back off, 20 do not), the hybrid formula, the walk-forward `lambda` and its tie-break, and the prequential property in three forms (the stored prediction equals the prediction of the history before it; changing a Reel never changes its own prediction; input order does not matter). `tests/test_prediction_evaluation.py` checks the scoring and the rule that nothing is claimed early: no comparison and no verdict before 1,500 (here: the configured minimum) scored Reels, the minimum is inclusive, a clearly better hybrid is reported with intervals, pure chance is not called a win, and the summary is strict JSON. `tests/test_prediction_store.py` (pgvector) checks storage (five rows per Reel, stored before the Reel is applied, one set after a rebuild, earlier rows unchanged by a new Reel, versions side by side, users isolated) and the endpoint (login, label, five models, distributions sum to 1, backoff explanation, no success wording, one fixed-size read).

## Deletion

`tests/test_deletion.py` (pgvector) does not trust the endpoint's report: after each deletion it counts rows in every table of the schema by independent queries and walks the frame directory. `test_every_table_is_accounted_for` fails when a table is added to the schema without being classified, so deletion cannot silently miss it. Scenarios: one session (its rows, jobs, frames and cache rows gone, the other session whole), derived data (predictions and transitions removed, snapshots equal to a fresh rebuild of what remains, unrebuildable snapshots dropped), the shared cache (kept while another user has the Reel, removed after), an active session, all data (zero everywhere, nothing of another user touched, login and extension cut off), idempotence, authorisation and CSRF, consent revocation stopping sessions. The verification itself is tested by breaking deletion on purpose (a file step that does nothing, a failing file step, a skipped cache sweep, surviving jobs): each must come back as `deletion_incomplete` with the right counts and be finished by a retry.

## Topic evidence

`tests/test_topic_evidence.py` (pgvector) is the authorisation and correctness suite for `GET /topics/{id}/evidence`. Authorisation: a login is required (an extension token is not one), the user comes from the cookie and not from any query parameter, another user's Reels, captions and ids never appear even when they watched the same Reel, and a user with no fingerprint gets nothing of anyone else's. Correctness: the listed contributions add up to the stored snapshot weight for both kinds and three topics (the reconciliation that makes the list "the reason"), each item's gain, recency and contribution equal the engine's own functions, skipped Reels (no assignment, no dwell) are absent, the snapshot's own settings and assignment version are used, Reels newer than the snapshot are excluded. Pagination: pages cover every Reel once in order, the edges (last page, past the end, exact fit), stable ties, default size. Cost: a fixed number of queries whatever the page. The panel is covered by Vitest and `dashboard/e2e/evidence.spec.ts`.

## Snapshots and history (M5)

`tests/test_fingerprint_engine.py` checks day and session snapshots: one per UTC day with a Reel, `taken_at` at that day's last Reel, midnight and UTC-offset handling, the prefix property (a day's snapshot equals a rebuild of the history up to that Reel), time order whatever the input order, per-bucket Reel counts, and reproducibility (two rebuilds, shuffled input, same JSON text, same version). `tests/test_fingerprint_store.py` (pgvector) checks replay: running the rebuild twice leaves the same rows, a later Reel adds a day and leaves earlier days untouched, versions do not overwrite each other, users are isolated, and the `rebuild-fingerprints` command. `tests/test_dashboard_reads.py` checks the history endpoint, including that it reads snapshots only in a constant number of queries.

## Dashboard browser tests (Playwright)

`dashboard/e2e/` runs the built dashboard in Chromium (desktop and Pixel 7 viewports) against a mocked API (`e2e/mock-api.ts`, stateful: data endpoints answer 401 until the fake login succeeds). Run: `cd dashboard && npm run e2e` (first time: `npx playwright install chromium`). It covers login, dashboard load, fingerprint display (shares add up to 100.0%), the session list, the History page (share over time, exposure versus engagement, daily snapshots, session timeline, the filter, and that it reads snapshots in a few requests), loading, empty and error states, and responsive layout. It does not start FastAPI or a database, so it cannot catch a mismatch between the mock and the real API; `tests/test_dashboard_reads.py` covers the real endpoints against pgvector.

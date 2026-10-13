# Privacy

Sources: spec F1, F13–F15, N6, N7, §7 and the privacy and security rules in `CONTRIBUTING.md`. This is the engineering privacy contract. The user-facing privacy notice (M7) must match it.

## What is collected

Only while a session is active, with consent, on the Reels surface:

| Data | Notes |
|---|---|
| Reel shortcode | Identifies the Reel |
| Creator handle, caption, hashtags | Each with `available` or `unavailable` status |
| `observed_at`, dwell time, video duration | Time as primary Reel while the tab is visible and the video plays |
| Up to 5 downscaled JPEG frames per Reel | Temporary; see Retention |

Derived on the server: OCR text, text and visual embeddings, topic distributions, fingerprints, predictions.

Given by you: if you answer the research survey, your 1 to 5 interest rating for each topic (skipping is allowed and recorded as a skip). It is used only to compare against your fingerprints for the evaluation, and is deleted with the rest of your data.

## What is never collected

- Instagram credentials, passwords, cookies or tokens.
- Instagram network responses or private API data.
- Anything outside the rendered DOM and video pixels of the Reels surface.
- Data from any other site or any other Instagram page.
- Likes, comments, or audio (out of MVP).

The extension performs no automation: no auto-scroll, auto-play, scripted navigation or simulated input. The user browses; the extension only observes.

## Consent

- Explicit consent screen in the extension popup, listing the collected data, what is never collected, when it runs and what the user controls. It must be agreed to (checkbox, then button) before Start is available (F1).
- Consent is recorded on the server with the policy version and the **server's** grant time. The text is data in `extension/src/consent/policy.ts`; a test ties its wording to the version, so changing the text means a new version and consent given for an older text no longer counts.
- Revocable at any time (popup, or dashboard via `DELETE /consent`). Revocation stops active sessions and uploads at once, deletes events still queued in the browser, and is retried until the server confirms it.
- Without consent the extension emits zero events: `emitEvent` is the only way into the upload queue and it refuses unless consent is active and a session is running. Tests check both the queue and the network. The server also refuses to start a session or accept events without consent (403 `consent_required`); events of a revoked user's sessions are refused even after a new grant (`session_inactive`).
- Consent history is not kept: one row per user, so a re-grant overwrites an earlier revocation time.

## Transparency

- A badge and an in-page marker are visible whenever collection is on (F14): the red `REC` toolbar badge, a red badge in the popup, and a small marker at the bottom left of the Reels page. The marker is shown only in the monitored tab, on the Reels surface, while consent holds and no stop is due.
- The content script is injected on instagram.com pages but does nothing except on `/reels/*`. It has no storage, no network access and no access to the extension token. It never scrolls, clicks, plays or navigates (ADR-022).
- Sessions start only when the user presses Start, on a Reels tab, with consent. They stop on user action, after 60 seconds off the Reels surface, on tab close, on browser restart, on consent revocation, or at 60 minutes (F2). The server enforces the 60 minute cap as well, and allows one active session per user.
- The extension sees tab addresses only for instagram.com (host permission), used for the leave-Reels rule. It makes network requests only to the REF server, from one module (`extension/src/api/client.ts`).

## Retention

| Data | Retention |
|---|---|
| Raw frames | Deleted right after feature extraction, or after 24 hours, whichever comes first (F15). Never stored in Postgres |
| Embeddings, OCR text, topic data, fingerprints | Kept until the user deletes them |
| Event data (metadata, dwell) | Kept until the user deletes it |
| Logs | Contain IDs and error classes only; no captions, OCR text, tokens or frame data |

## Deletion

- **Delete a session** (`DELETE /sessions/{id}`) removes that session's Reels, jobs and frames, deletes the predictions, topic transitions and fingerprint snapshots (they are cumulative over your whole history), and rebuilds your fingerprints from the Reels that remain.
- **Delete all my data** (`DELETE /me/data`, the Account page) leaves zero rows and zero files (F13), account included. The server checks every table and the frame folder after deleting and reports success only when each count is zero; if anything is left it says so, and repeating the request finishes the job. The text-derived data of a Reel (embeddings, OCR text, topic scores) belongs to you alone and is removed with your exposures or your account; it is never read for another user and never kept for one after you delete (ADR-024).
- Deletion covers jobs, predictions, snapshots and orphaned cache rows.
- Revoking an extension token stops that device immediately.

## Isolation and access

- Every row is tied to a user. The server derives `user_id` from the credential and checks ownership on every request.
- Passwords are hashed with argon2id. Extension tokens are stored hashed (SHA-256) and shown once.
- TLS whenever the server is not on localhost. Local mode binds to `127.0.0.1`.
- Dashboard users are invited; there is no public signup.

## Research use

- Exposure is not preference. The limitations (desktop-only sample, taxonomy bias, small number of users) are stated in the MVP write-up.
- Cohort use (more than the developer) needs ethics and consent paperwork before any non-developer data is collected. This is open owner decision D-1.

## Open issues

Proposed in `docs/architecture-decisions.md`, not yet accepted:

- ~~Shared feature cache across users versus "zero rows" on deletion (ADR-007)~~: resolved by a per-user cache (ADR-024, Proposed).
- Deletion semantics for session delete, jobs, logs and aggregates (ADR-012).
- Hosted backups: the privacy notice must state how long backups persist after deletion (ADR-012, M7).

[HIGH] One user can poison the shared Reel cache for every other user

**Fixed on 2026-10-09** by making the Reel cache per user (ADR-024, migration `0006`). Tests: `tests/test_cache_isolation.py`.

**Severity:** HIGH (latent until the job consumer exists). **Area:** data ownership, privacy, research integrity. **Source:** `docs/security-audit.md`, H-3.

## Problem

Text features and topic assignments are cached per `(reel_shortcode, pipeline_version)` and shared across users (ADR-007, Proposed). The text that is embedded comes from the caption and hashtags of the viewer's own event, which the client wrote (`text_input_from_exposure` in `server/app/worker/store.py`). The first event processed for a shortcode writes the row (`ON CONFLICT DO NOTHING`; the module docstring says "The text of the first viewer's event is the text that is cached"). Later viewers of the same shortcode receive that assignment. Nothing ties the caption to the shortcode, and the shortcode is only format-checked (`^[A-Za-z0-9_-]{1,64}$` in `server/app/routers/events.py`), never verified.

This is established by reading the code. It is not exploitable today only because no worker consumes the job queue (audit finding M-8); the processing functions exist and are what the consumer will call.

## Impact

- An invited user (a token and consent are enough) can submit an event for the shortcode of a Reel that others will watch, with a caption chosen to land in a topic of their choice. If it is processed first, every later user's exposure to that Reel gets that topic. Their fingerprints, history, predictions and the study's evaluation are shaped by someone else.
- A user's text-derived data (embedding, OCR text) stays attached to a Reel that another user still has after the first user deletes their data (ADR-007).

## Suggested fix (not applied)

Do not let client-supplied text decide a row other users read. In order of preference:

1. Key the cache by the content that produced it: shortcode plus a hash of the normalised caption, hashtags and OCR text, so different text gets a different row.
2. Compute features per exposure and cache only what is a function of the shortcode alone.
3. Require two independent viewers to agree before a row is trusted.

Record which user produced a cache row. Decide ADR-007 and ADR-010 with this threat in mind.

## Acceptance

- A test where two users send different captions for the same shortcode: each gets an assignment computed from their own text, and neither affects the other.
- A test that deleting the first user's data leaves no text-derived data of theirs on the other user's Reel.

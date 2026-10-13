# Deterministic fingerprint replay

`python -m app.manage replay-fingerprint --pipeline-version FP [--user UUID] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--rel-tol X] [--abs-tol X] [--json]`

It rebuilds a user's exposure and engagement fingerprints, session snapshots and daily snapshots from the stored topic assignments, and compares every one with the stored snapshots. It only reads: a mismatch is reported, never repaired (`rebuild-fingerprints` repairs). CONTRIBUTING.md requires this property: "replay from stored assignments must reproduce stored snapshots exactly".

## Inputs

- **User**: `--user`, or every user that has snapshots of the version.
- **Pipeline version**: the fingerprint `pipeline_version` of the stored snapshots (`fp1-...`). The assignment version and every parameter (half-life, gain bounds, minimum duration, skip rule) are read from the `provenance` stored in those snapshots, never from today's defaults. The provenance must hash back to the version asked for; snapshots with no provenance, with provenance that disagrees with each other, that cannot be read, or that do not hash to the version are an error (`no_provenance`, `inconsistent_provenance`, `bad_provenance`, `provenance_does_not_match_version`) and are not guessed.
- **Stored assignments and exposures**: the user's Reels in `(observed_at, event_id)` order (ADR-011), with the topic distributions of that assignment version.
- **`--from` / `--to`**: UTC days, inclusive, choosing which snapshots are compared. The replay always starts from the user's first Reel, because every weight depends on the whole history before it; a window never changes a number. A snapshot is in the window when either its stored or its replayed day is.

## What is compared

Per snapshot (`(kind, session)` or `(kind, UTC day)`): `taken_at`, `n_reels`, Reels in the bucket, last observation time, provenance, schema, and every raw weight and displayed share per topic. A topic on one side only is a difference. A snapshot on one side only (a Reel deleted or added since, a stored row missing or extra) is a difference, never skipped.

## Tolerance

Floats match when `|a - b| <= max(abs_tol, rel_tol * max(|a|, |b|))`, default `rel_tol = 1e-9`, `abs_tol = 1e-12`. NaN and infinity never match. Integers, times and provenance must be equal. The tolerance is for other platforms and libraries: on the same code and data the replay is bit-identical, so an observed difference above zero is already a signal. The report states the largest absolute and relative differences found even when everything matches. The replay also rebuilds twice, once with the Reels in reverse order, and reports `deterministic` (identical snapshots); a false value is a mismatch.

## Output and exit codes

One line per user (ids, counts and numbers only, never Reel content), then up to 50 differences with the stored and replayed number. `--json` prints strict JSON (`format: fingerprint-replay/1`).

- `0`: every user matched.
- `1`: at least one mismatch.
- `2`: could not replay: no snapshots of that version, an error reason above, `inputs_rejected_by_engine` (a stored distribution no longer sums to 1), `from_after_to`, or bad arguments.

## Not covered

Predictions and topic transitions are rebuilt by `rebuild-fingerprints` and have their own tests; this command checks fingerprints and snapshots only. Replay cannot show that the stored assignments were right, only that the snapshots follow from them.

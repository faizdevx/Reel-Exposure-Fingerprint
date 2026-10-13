"""Worker pipeline (spec §5, M3). Built stage by stage; each stage reports on itself.

Layout:

- `stages.py`: `StageRunner` and `StageReport`. Every stage records `started_at`, `finished_at`,
  `duration_ms`, `status` (`ok`, `failed`, `skipped`) and `error` (the error class name only).
  A stage that fails does not stop the others: stages that do not depend on it still run.
- `text_normalize.py`: caption, hashtags and OCR text to one normalised string.
- `embedder.py`: `multilingual-e5-small` text embeddings.
- `topic_embeddings.py`: taxonomy embeddings, cached on disk.
- `scoring.py`: zero-shot topic probabilities, `softmax(cos / tau)` (spec §5.2).
- `text_pipeline.py`: the stages above in order, with the per-stage reports.
- `store.py`: writes `content_features` and `topic_assignments`, keyed by `pipeline_version`.
- `config.py`: the pipeline configuration and `pipeline_version` (N9).

Not built yet: the job loop that claims `jobs` rows, OCR, visual embeddings, fusion and the user
fingerprint update. Nothing here writes a fingerprint.

Logs and reports carry stage names, durations, IDs and error classes only. Never captions,
hashtags, OCR text or any message text from an exception (CONTRIBUTING.md, privacy rules).
"""

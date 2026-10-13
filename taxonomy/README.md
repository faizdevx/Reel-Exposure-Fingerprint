# taxonomy/

Versioned topic taxonomy for zero-shot topic assignment (spec §5.2).

- `topics.json`: **taxonomy v1**, 28 topics. Each topic has `id`, `slug`, `label`, `taxonomy_version`, and 4 short descriptions in English and 4 in Hindi. Descriptions are the text that the encoders embed; a Reel is scored by cosine similarity to each topic's embeddings. Nothing is fitted or clustered.
- The catch-all `other` of spec §5.2 is not listed. It is assigned by the fusion threshold, so it has no descriptions. The slug `other` is reserved.
- The loader and validator are `server/app/taxonomy.py`; tests are `tests/test_taxonomy.py`.

Rules:

- Valid means: 25 to 30 topics; unique `id` (positive, smallint range), unique `slug` and `label`; every topic carries the file's `taxonomy_version`; 3 to 5 non-empty descriptions per language, none repeated inside a topic or shared between topics; Hindi descriptions mostly Devanagari, English ones with no Devanagari.
- **Any change to a topic, label or description needs a new `taxonomy_version`** (it is part of the pipeline version, CONTRIBUTING.md). `tests/test_taxonomy.py` pins a content hash to the version and fails until both are updated together.
- **Provisional:** v1 is hand-written. Owner decision D-4 (hand-written or adapted from an existing taxonomy) is still open, and the topic list and Hindi wording have not been reviewed by a Hindi speaker. The M3 label audit (roadmap E6) decides whether v1 becomes v2.
- No clustering or topic modelling (KMeans, MiniBatchKMeans, BERTopic, HDBSCAN) is part of the MVP; a test checks that none is a dependency.

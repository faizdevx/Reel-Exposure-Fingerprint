"""Fingerprint engine (spec §5.2, F10, N9).

Two fingerprints per user, `exposure` and `engagement`. Each is a topic-weight vector `W` (one
non-negative weight per topic), never a mean of embeddings. Per applied Reel, with `p` the Reel's
topic distribution and `dt` the time since the previous applied Reel:

    W_k <- W_k * 2^(-dt / h) + g * p(k)

Display value: `W_k / sum(W)`.

Layout:

- `engine.py`: configuration, `fingerprint_version`, the gain functions, the update step, and
  `rebuild`, the one replay function that produces final states and session snapshots.
- `store.py`: reads observations from the database and writes snapshot rows.

Not built: the job loop and the per-user state table (ADR-011, Proposed), daily snapshots, the
prediction models. Nothing here reads the wall clock, a random source or the environment.
"""

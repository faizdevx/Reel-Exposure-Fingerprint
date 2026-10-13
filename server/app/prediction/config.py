"""Prediction settings and their version (spec N9).

`prediction_version` hashes every setting and the fingerprint version it builds on (which in turn
covers the topic assignments), so a prediction row says exactly what produced it. Change a setting
and the version changes; rows of the old version are never rewritten.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

#: The only way a prediction may be named. It is about what the feed shows next, not about taste.
PREDICTION_LABEL = "Predicted next observed topic"
PREDICTION_NOTE = (
    "A statistical guess at the topic of the next Reel the feed shows you, from your own history. "
    "It is not a prediction of what you like."
)

MODELS = ("uniform", "most_frequent", "decayed_fingerprint", "markov", "hybrid")
BASELINES = ("uniform", "most_frequent", "decayed_fingerprint")

PREDICTION_SCHEMA = "prediction/1"


@dataclass(frozen=True)
class PredictionConfig:
    #: Add-one smoothing (spec §5.2): `(count + a) / (total + a * topics)`.
    smoothing: float = 1.0
    #: A transition row with fewer observations than this is not trusted (spec §5.2).
    min_row_observations: int = 20
    #: Weight of the fingerprint in the hybrid until `lambda` can be chosen from the data.
    lambda_default: float = 0.5
    lambda_grid: tuple[float, ...] = (0.1, 0.3, 0.5, 0.7, 0.9)
    #: Earlier Reels needed before `lambda` is chosen from them.
    min_events_to_tune: int = 100
    #: Reels in the rolling accuracy figures.
    rolling_window: int = 100
    #: Scored Reels before any comparison between models is stated (spec §10).
    min_events_for_conclusion: int = 1500
    bootstrap_resamples: int = 1000
    bootstrap_seed: int = 0

    def __post_init__(self) -> None:
        if self.smoothing <= 0.0:
            raise ValueError("smoothing must be positive")
        if self.min_row_observations < 1 or self.min_events_to_tune < 1:
            raise ValueError("minimums must be at least 1")
        if not self.lambda_grid or any(not 0.0 < v < 1.0 for v in self.lambda_grid):
            raise ValueError("lambda_grid must be non-empty values strictly between 0 and 1")
        if not 0.0 < self.lambda_default < 1.0:
            raise ValueError("lambda_default must be strictly between 0 and 1")
        if self.rolling_window < 1 or self.min_events_for_conclusion < 1:
            raise ValueError("window and conclusion minimum must be at least 1")


def canonical_prediction_config(
    config: PredictionConfig, fingerprint_version: str
) -> dict[str, object]:
    return {
        "schema": PREDICTION_SCHEMA,
        "task": "topic of the next observed Reel; topic = argmax of the fused distribution",
        "models": list(MODELS),
        "fingerprint_version": fingerprint_version,
        "fingerprint_used": "exposure",
        "most_frequent": "add-one smoothed counts of observed topics",
        "markov": "add-one smoothed row of the last topic; fingerprint while the row is short",
        "hybrid": "lambda * P_fingerprint + (1 - lambda) * P_transition, same fallback",
        "lambda_choice": "lowest cumulative log loss on earlier Reels; default until enough exist",
        "params": asdict(config),
    }


def prediction_version(config: PredictionConfig, fingerprint_version: str) -> str:
    blob = json.dumps(
        canonical_prediction_config(config, fingerprint_version),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"pr1-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"

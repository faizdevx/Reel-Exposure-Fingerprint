"""The real SigLIP models: shapes, determinism, order independence. Needs the model files.

Skipped unless `REF_TEST_MODEL=1` (and `uv sync --group ml`). Set HF_HOME to a cache with space;
the first run downloads about 0.8 GB per model.
"""

import io
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.taxonomy import load_taxonomy
from app.worker.vision_config import (
    SIGLIP2_BASE,
    SIGLIP_MULTILINGUAL,
    VisionModelConfig,
    VisualPipelineConfig,
)
from app.worker.visual_embedder import FrameDecodeError, SigLipEmbedder
from app.worker.visual_pipeline import VisualPipeline

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(os.environ.get("REF_TEST_MODEL") != "1", reason="REF_TEST_MODEL=1 not set"),
]

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"


def jpeg(color: tuple[int, int, int], size: tuple[int, int] = (320, 480)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=70)
    return out.getvalue()


@pytest.fixture(scope="module", params=[SIGLIP2_BASE, SIGLIP_MULTILINGUAL], ids=lambda m: m.key)
def embedder(request: pytest.FixtureRequest) -> Iterator[SigLipEmbedder]:
    model: VisionModelConfig = replace(request.param, threads=4)
    yield SigLipEmbedder(model)


def test_image_vectors_are_768_unit_float32(embedder: SigLipEmbedder) -> None:
    vectors = embedder.embed_images([jpeg((200, 30, 30)), jpeg((30, 30, 200))])
    assert vectors.shape == (2, 768) and vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)
    assert np.isfinite(vectors).all()
    assert float(vectors[0] @ vectors[1]) < 0.999  # different pictures, different vectors


def test_the_same_frame_gives_the_same_vector(embedder: SigLipEmbedder) -> None:
    frame = jpeg((120, 200, 60))
    np.testing.assert_array_equal(embedder.embed_images([frame]), embedder.embed_images([frame]))


def test_a_vector_does_not_depend_on_the_other_frames(embedder: SigLipEmbedder) -> None:
    a, b = jpeg((200, 30, 30)), jpeg((30, 200, 30))
    alone = embedder.embed_images([a])
    together = embedder.embed_images([b, a])[1:]
    np.testing.assert_allclose(alone, together, atol=1e-6)


def test_the_text_tower_shares_the_dimension_and_is_unit_length(embedder: SigLipEmbedder) -> None:
    vectors = embedder.embed_texts(["this is a photo of a cricket match.", "क्रिकेट मैच"])
    assert vectors.shape == (2, 768)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)


def test_a_picture_is_closer_to_its_description_than_to_an_unrelated_one(
    embedder: SigLipEmbedder,
) -> None:
    image = embedder.embed_images([jpeg((220, 20, 20))])[0]
    texts = embedder.embed_texts(["this is a photo of a red picture.", "this is a photo of a dog."])
    assert float(image @ texts[0]) > float(image @ texts[1])


def test_bytes_that_are_not_an_image_are_a_frame_error(embedder: SigLipEmbedder) -> None:
    with pytest.raises(FrameDecodeError):
        embedder.embed_images([b"\xff\xd8\xff\xe0 not really a jpeg \xff\xd9"])


def test_the_full_pipeline_on_the_real_model(embedder: SigLipEmbedder) -> None:
    taxonomy = load_taxonomy(TAXONOMY_FILE)
    config = VisualPipelineConfig(embedder._config)
    pipeline = VisualPipeline.create(config, taxonomy, embedder, None)
    frames = [jpeg((200, 30, 30)), jpeg((30, 200, 30)), jpeg((30, 30, 200))]
    first = pipeline.run(frames)
    second = pipeline.run(list(reversed(frames)))
    assert first.scores is not None and first.embedding is not None
    assert sum(first.scores.probabilities) == pytest.approx(1.0)
    assert len(first.scores.probabilities) == len(taxonomy.topics)
    np.testing.assert_array_equal(first.embedding, second.embedding)  # order does not matter
    assert first.scores == second.scores

"""Image and text embeddings from one SigLIP model (spec §5.2, §6).

SigLIP has two towers that share one space: the image tower embeds frames, the text tower embeds
the taxonomy descriptions. Topic scoring compares an image vector with text-tower vectors of the
same model, never with e5 vectors: the two encoders have different spaces and sizes, so e5 and
SigLIP vectors are never added, averaged or compared with each other. Modalities meet only as
probabilities (`app.worker.fusion`).

`ImageEmbedder` is the interface the pipeline uses; `SigLipEmbedder` is the real model. Tests use a
fake with the same interface. torch, transformers and Pillow are optional (`uv sync --group ml`) and
imported when the model is first used.

Frames arrive as JPEG bytes and are decoded here, one at a time, so a frame that cannot be decoded
is an error for that frame only. Preprocessing is the model's own image processor, so the numbers
match what the model was trained with. Inference runs in eval mode under `torch.inference_mode()`
with a fixed thread count and no sampling. Images are embedded in batches of one: a vector never
depends on the other frames processed with it.
"""

import io
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.worker.embedder import EmbeddingError, Vectors, l2_normalize
from app.worker.vision_config import VisionModelConfig

log = logging.getLogger("app.worker.visual_embedder")

# A frame is at most 150 KB of JPEG (spec §8); refuse a decoded picture far larger than any
# downscaled frame so a crafted file cannot exhaust memory.
MAX_DECODED_PIXELS = 4096 * 4096


class FrameDecodeError(EmbeddingError):
    """One frame could not be decoded. Carries no image content."""


class ImageEmbedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed_images(self, jpegs: Sequence[bytes]) -> Vectors:
        """One unit-length float32 vector per JPEG, shape `(len(jpegs), dim)`.

        Raises `FrameDecodeError` if any frame cannot be decoded; callers that want to keep going
        pass frames one at a time.
        """
        ...

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        """One unit-length float32 vector per text from the text tower, same space and `dim`."""
        ...


class SigLipEmbedder:
    def __init__(
        self,
        config: VisionModelConfig,
        *,
        cache_dir: Path | None = None,
        local_files_only: bool = False,
    ) -> None:
        self._config = config
        self._cache_dir = cache_dir
        self._local_only = local_files_only
        self._torch: Any = None
        self._processor: Any = None
        self._model: Any = None

    @property
    def dim(self) -> int:
        return self._config.dim

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            import transformers
            from transformers import AutoModel, AutoProcessor
        except ImportError as error:
            raise EmbeddingError("torch and transformers are not installed") from error

        torch.set_num_threads(self._config.threads)
        kwargs: dict[str, Any] = {
            "revision": self._config.revision,
            "cache_dir": None if self._cache_dir is None else str(self._cache_dir),
            "local_files_only": self._local_only,
        }
        processor = AutoProcessor.from_pretrained(  # type: ignore[no-untyped-call]
            self._config.model_id, **kwargs
        )
        model = AutoModel.from_pretrained(self._config.model_id, **kwargs).eval()
        self._torch, self._processor, self._model = torch, processor, model
        log.info(
            "visual model loaded model=%s revision=%s license=%s torch=%s transformers=%s",
            self._config.model_id,
            self._config.revision,
            self._config.license,
            torch.__version__,
            transformers.__version__,
        )

    @staticmethod
    def _as_array(out: Any, torch: Any) -> Vectors:
        # transformers 5 can return a ModelOutput instead of a tensor.
        tensor = out
        if not isinstance(out, torch.Tensor):
            for key in ("pooler_output", "image_embeds", "text_embeds"):
                tensor = getattr(out, key, None)
                if tensor is not None:
                    break
            else:
                raise EmbeddingError("unexpected model output")
        array: Vectors = tensor.float().cpu().numpy().astype(np.float32)
        return array

    def _decode(self, jpeg: bytes) -> Any:
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise FrameDecodeError("frame is too large")
                return image.convert("RGB")
        except FrameDecodeError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            raise FrameDecodeError("frame could not be decoded") from error

    def embed_images(self, jpegs: Sequence[bytes]) -> Vectors:
        if not jpegs:
            return np.zeros((0, self._config.dim), dtype=np.float32)
        self._load()
        rows: list[Vectors] = []
        for jpeg in jpegs:
            image = self._decode(jpeg)
            inputs = self._processor(images=[image], return_tensors="pt")
            with self._torch.inference_mode():
                out = self._model.get_image_features(pixel_values=inputs["pixel_values"])
            rows.append(self._as_array(out, self._torch))
        vectors = l2_normalize(np.concatenate(rows, axis=0))
        if vectors.shape != (len(jpegs), self._config.dim):
            raise EmbeddingError("unexpected image embedding shape")
        return vectors

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.zeros((0, self._config.dim), dtype=np.float32)
        self._load()
        prepared = [t.lower() if self._config.lowercase_text else t for t in texts]
        inputs = self._processor(
            text=prepared,
            padding="max_length",
            max_length=self._config.text_max_length,
            truncation=True,
            return_tensors="pt",
        )
        with self._torch.inference_mode():
            out = self._model.get_text_features(**inputs)
        vectors = l2_normalize(self._as_array(out, self._torch))
        if vectors.shape != (len(texts), self._config.dim):
            raise EmbeddingError("unexpected text embedding shape")
        return vectors

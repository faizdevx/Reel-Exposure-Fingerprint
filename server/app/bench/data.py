"""Generated benchmark inputs: captions and frames. Fixed seeds, nothing from Instagram.

Frames are 540 x 960 JPEGs (quality 70), the size class of spec F7 ("downscale + JPEG q70"): a
smooth photo-like background with one to four lines of overlaid English text. They are NOT real
Reel frames. Latency of the encoders does not depend on picture content (the image is resized to
a fixed input size), but OCR time depends on how much text there is and on its contrast, so OCR
numbers on these frames are a reproducible reference, not an estimate for real frames
(`docs/ocr-evaluation.md`: real frames are still missing). Pass `--frames DIR` to use your own.
"""

import io
import random
from pathlib import Path

import numpy as np

WIDTH, HEIGHT = 540, 960
JPEG_QUALITY = 70

_EN = [
    "When your teacher says there is a surprise test tomorrow",
    "Easy paneer butter masala recipe, ready in twenty minutes",
    "Kohli hits a six in the last over, IPL cricket highlights",
    "Five morning exercises that fix your back pain at home",
    "How I learned Python in three months while working full time",
    "Street food tour of Delhi: chaat, kulfi and the best jalebi",
    "Budget travel tips for a week in Goa under ten thousand rupees",
    "New phone launch: camera, battery and price explained",
    "Dance challenge with my sister, we practised all weekend",
    "Skincare routine for oily skin in the monsoon season",
]
_HINGLISH = [
    "Aaj ka match dekha? Last over mein kya six mara yaar",
    "Ghar pe banao crispy samosa, bilkul market jaisa",
    "Gym jaane ka mann nahi tha lekin phir bhi gaya, motivation chahiye",
    "Delhi se Manali road trip ka poora plan, budget ke saath",
    "Exam se pehle ye 5 tips zaroor follow karo",
]
_HINDI = [
    "जब टीचर बोले कल टेस्ट है",
    "घर पर बनाएं आसान पनीर की सब्ज़ी",
    "आज का मैच देखा आपने",
    "सुबह की सैर के पांच फायदे",
    "दिल्ली से मनाली की यात्रा",
]
_TAGS = [
    "cricket",
    "recipe",
    "fitness",
    "travel",
    "tech",
    "comedy",
    "dance",
    "beauty",
    "food",
    "reels",
]


def captions(n: int, seed: int = 0) -> list[tuple[str, tuple[str, ...]]]:
    """`n` (caption, hashtags) pairs, English, Hinglish and Hindi in a fixed 6:3:3 mix."""
    rng = random.Random(seed)  # noqa: S311  (a reproducible benchmark input, not security)
    pool = _EN * 3 + _HINGLISH * 2 + _HINDI * 2
    out = []
    for _ in range(n):
        text = rng.choice(pool)
        tags = tuple(rng.sample(_TAGS, rng.randint(1, 4)))
        out.append((f"{text} #{' #'.join(tags)}", tags))
    return out


def make_frame(index: int, *, lines: int | None = None, seed: int = 0) -> bytes:
    """One synthetic JPEG frame, deterministic in `(index, seed)`."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = np.random.default_rng(seed * 100_003 + index)
    small = rng.integers(40, 215, size=(12, 7, 3), dtype=np.uint8)
    image = Image.fromarray(small).resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)
    image = image.filter(ImageFilter.GaussianBlur(4))
    # Fine grain, so the JPEG is the size of a real photo frame and not of a smooth gradient.
    grain = rng.normal(0.0, 9.0, size=(HEIGHT, WIDTH, 1))
    image = Image.fromarray(np.clip(np.asarray(image) + grain, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    count = lines if lines is not None else 1 + index % 4
    pick = random.Random(index * 7919 + seed)  # noqa: S311
    y = 120 + pick.randint(0, 200)
    for _ in range(count):
        text = pick.choice(_EN)[: pick.randint(18, 34)]
        draw.text(
            (36, y), text, fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0)
        )
        y += 70
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def load_frames(directory: Path | None, n: int, seed: int = 0) -> list[bytes]:
    """`n` frames: the JPEGs of `directory` (cycled) if given, else generated."""
    if directory is not None:
        files = sorted(p for p in directory.iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
        if not files:
            raise FileNotFoundError(f"no JPEG files in {directory}")
        return [files[i % len(files)].read_bytes() for i in range(n)]
    return [make_frame(i, seed=seed) for i in range(n)]

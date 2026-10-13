"""M0 throwaway: build representative Reel-like frames for the ML bench.

Composites COCO val2017 photos (spikes/data/coco, downloaded separately) into
512x910 portrait frames with English / Hindi / mixed overlay text, plus
text-only cards. Text is rendered by headless Chrome because Pillow on this
machine lacks libraqm, so it cannot shape Devanagari correctly.

Usage:
    .venv/Scripts/python.exe -I make_samples.py --chrome "<path to chrome.exe>" --tmp <scratch dir>

Output: spikes/data/synthetic_frames/*.jpg + manifest.json (gitignored).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import subprocess
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
COCO = ROOT / "data" / "coco"
OUT = ROOT / "data" / "synthetic_frames"
W, H = 512, 910

# Acceptable topics per photo for the zero-shot sanity check (eyeballed).
PHOTOS = {
    "000000000139": [],  # living room
    "000000000285": ["pets_animals"],  # bear
    "000000000632": [],  # bedroom with bookshelf
    "000000000724": [],  # stop sign, street
    "000000000776": [],  # teddy bears
    "000000000785": ["travel", "cricket_sports"],  # skier
    "000000000872": ["cricket_sports"],  # baseball
    "000000000885": ["cricket_sports"],  # tennis
    "000000001000": ["cricket_sports"],  # kids on a tennis court
    "000000001268": ["travel"],  # river under a bridge, tourist
    "000000001296": ["tech_coding"],  # woman using a phone
    "000000039769": ["pets_animals"],  # two cats
}

OVERLAYS_EN = [
    "Wait for the end",
    "Day 1 vs Day 30",
    "Rate this 1-10",
    "Best place to visit",
    "Don't skip this",
    "My morning routine",
    "Try this trick",
    "Part 2 in comments",
    "Nobody talks about this",
    "Watch till end",
    "Save for later",
    "Weekend vibes",
]
OVERLAYS_HI = [
    "अंत तक देखें",
    "पहला दिन बनाम तीसवां दिन",
    "इसे 1 से 10 रेटिंग दें",
    "घूमने की सबसे अच्छी जगह",
    "इसे स्किप मत करना",
    "मेरी सुबह की दिनचर्या",
    "यह तरीका आज़माएं",
    "भाग 2 कमेंट में",
    "इसके बारे में कोई बात नहीं करता",
    "आखिर तक देखो",
    "बाद के लिए सेव करें",
    "वीकेंड का मज़ा",
]
CARDS = [
    ("en", "5 habits that changed my life"),
    ("hi", "सफलता के 5 नियम"),
    ("mixed", "Paisa bachane ke 3 tarike / पैसे बचाने के तरीके"),
    ("en", "Python tip: use enumerate()"),
    ("hi", "आज का सुविचार: मेहनत ही सफलता की कुंजी है"),
    ("mixed", "Exam tips / परीक्षा टिप्स"),
]


def page(bg_data_url: str | None, text: str, card: bool) -> str:
    bg = f"background:url('{bg_data_url}') center/cover;" if bg_data_url else "background:#111;"
    pos = "top:40%;" if card else "top:12%;"
    size = "40px" if card else "34px"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{W}px;height:{H}px;overflow:hidden;{bg}}}
.t{{position:absolute;left:24px;right:24px;{pos}text-align:center;
font:700 {size}/1.3 'Nirmala UI','Segoe UI',sans-serif;color:#fff;
background:rgba(0,0,0,.45);padding:10px 12px;border-radius:10px}}
</style></head><body><div class="t">{html.escape(text)}</div></body></html>"""


def render(chrome: str, tmp: Path, name: str, doc: str) -> Path:
    src = tmp / f"{name}.html"
    png = tmp / f"{name}.png"
    src.write_text(doc, encoding="utf-8")
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            f"--user-data-dir={tmp / 'chrome-profile'}",  # never the user's profile
            "--force-device-scale-factor=1",
            f"--window-size={W},{H}",
            f"--screenshot={png}",
            src.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    # On Windows the launcher can exit before the screenshot is flushed.
    for _ in range(150):
        if png.exists() and png.stat().st_size > 0:
            time.sleep(0.3)
            break
        time.sleep(0.1)
    out = OUT / f"{name}.jpg"
    Image.open(png).convert("RGB").crop((0, 0, W, H)).save(out, "JPEG", quality=70)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True)
    ap.add_argument("--tmp", type=Path, required=True)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    a.tmp.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, (pid, topics) in enumerate(PHOTOS.items()):
        data = base64.b64encode((COCO / f"{pid}.jpg").read_bytes()).decode()
        url = f"data:image/jpeg;base64,{data}"
        for lang, text in (("en", OVERLAYS_EN[i]), ("hi", OVERLAYS_HI[i])):
            name = f"photo_{pid[-4:]}_{lang}"
            render(a.chrome, a.tmp, name, page(url, text, card=False))
            manifest.append(
                {
                    "file": f"{name}.jpg",
                    "kind": "photo",
                    "photo": pid,
                    "topics": topics,
                    "overlayLang": lang,
                    "overlay": text,
                }
            )
    for j, (lang, text) in enumerate(CARDS):
        name = f"card_{j}_{lang}"
        render(a.chrome, a.tmp, name, page(None, text, card=True))
        manifest.append(
            {
                "file": f"{name}.jpg",
                "kind": "card",
                "photo": None,
                "topics": [],
                "overlayLang": lang,
                "overlay": text,
            }
        )
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {len(manifest)} frames to {OUT}")


if __name__ == "__main__":
    main()

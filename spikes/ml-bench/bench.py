"""M0 throwaway: CPU feasibility bench for e5 text, SigLIP image and RapidOCR.

Usage:
    set HF_HOME to a cache on a disk with space, then
    .venv/Scripts/python.exe -I bench.py --threads 4 [--frames <dir with manifest.json>]

Writes results/bench-<threads>t-<timestamp>.json and prints a summary.
Sanity accuracies here use synthetic samples and are NOT the M3/M4 label audit.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FRAMES = HERE.parent / "data" / "synthetic_frames"
E5_ID = "intfloat/multilingual-e5-small"
SIGLIP_IDS = ["google/siglip2-base-patch16-224", "google/siglip-base-patch16-256-multilingual"]
SIGLIP_TEMPLATE = "this is a photo of {}."


def ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {
        "n": len(s),
        "p50": round(statistics.median(s), 1),
        "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 1),
        "max": round(s[-1], 1),
    }


def topk_hits(scores, labels: list[str], acceptable: list[list[str]]) -> dict:
    import torch

    top = torch.topk(scores, k=3, dim=1).indices.tolist()
    n = len(acceptable)
    h1 = sum(1 for t, ok in zip(top, acceptable) if labels[t[0]] in ok)
    h3 = sum(1 for t, ok in zip(top, acceptable) if any(labels[i] in ok for i in t))
    return {
        "n": n,
        "top1": round(h1 / n, 3) if n else None,
        "top3": round(h3 / n, 3) if n else None,
        "predicted": [[labels[i] for i in t] for t in top],
    }


# ---------------------------------------------------------------- e5 text


def bench_e5(texts: dict, threads: int) -> dict:
    import torch
    import torch.nn.functional as F
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoTokenizer

    r: dict = {"model": E5_ID}
    t0 = time.perf_counter()
    path = snapshot_download(E5_ID, allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt"])
    r["downloadOrCacheMs"] = ms(t0)
    r["revision"] = Path(path).name
    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(path)
    model = AutoModel.from_pretrained(path).eval()
    r["loadMs"] = ms(t0)

    @torch.inference_mode()
    def embed(batch: list[str]):
        enc = tok(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
        out = model(**enc).last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).float()
        pooled = (out * mask).sum(1) / mask.sum(1)
        return F.normalize(pooled, dim=-1)

    labels = list(texts["topics"].keys())
    topic_vecs = []
    for k in labels:
        v = embed([f"passage: {d}" for d in texts["topics"][k]]).mean(0)
        topic_vecs.append(F.normalize(v, dim=0))
    topic_mat = torch.stack(topic_vecs)
    r["dim"] = int(topic_mat.shape[1])

    caps = [c for c in texts["captions"] if c["text"].strip()]
    r["emptyCaptionsSkipped"] = len(texts["captions"]) - len(caps)
    queries = [f"query: {c['text']}" for c in caps]
    embed(queries[:2])  # warm-up

    per_item = []
    single = []
    for q in queries:
        t0 = time.perf_counter()
        single.append(embed([q]))
        per_item.append(ms(t0))
    r["latencyBatch1Ms"] = summary(per_item)
    t0 = time.perf_counter()
    batched = embed(queries)
    r["batchAllMs"] = ms(t0)
    r["batchAllN"] = len(queries)
    again = embed(queries)
    r["determinism"] = {
        "sameBatchMaxAbsDiff": float((batched - again).abs().max()),
        "batch1VsBatchedMaxAbsDiff": float((torch.cat(single) - batched).abs().max()),
    }

    labelled = [(i, c) for i, c in enumerate(caps) if c["topic"]]
    scores = batched[[i for i, _ in labelled]] @ topic_mat.T
    hits = topk_hits(scores, labels, [[c["topic"]] for _, c in labelled])
    r["sanityZeroShot"] = {k: v for k, v in hits.items() if k != "predicted"}
    by_lang: dict = {}
    for (i, c), pred in zip(labelled, hits["predicted"]):
        d = by_lang.setdefault(c["lang"], {"n": 0, "top1": 0, "top3": 0})
        d["n"] += 1
        d["top1"] += pred[0] == c["topic"]
        d["top3"] += c["topic"] in pred
    r["sanityByLang"] = by_lang
    r["misses"] = [
        {"text": c["text"][:60], "want": c["topic"], "got": p[:3]}
        for (_, c), p in zip(labelled, hits["predicted"])
        if p[0] != c["topic"]
    ]
    return r


# ---------------------------------------------------------------- SigLIP


def bench_siglip(model_id: str, texts: dict, frames: list[dict], frame_dir: Path) -> dict:
    import torch
    import torch.nn.functional as F
    from huggingface_hub import snapshot_download
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    r: dict = {"model": model_id}
    try:
        t0 = time.perf_counter()
        path = snapshot_download(
            model_id, allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt"]
        )
        r["downloadOrCacheMs"] = ms(t0)
        r["revision"] = Path(path).name
        t0 = time.perf_counter()
        proc = AutoProcessor.from_pretrained(path)
        model = AutoModel.from_pretrained(path).eval()
        r["loadMs"] = ms(t0)
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        return r

    def as_tensor(out):
        # transformers 5 may return a ModelOutput instead of a tensor.
        if isinstance(out, torch.Tensor):
            return out
        for key in ("pooler_output", "image_embeds", "text_embeds"):
            if getattr(out, key, None) is not None:
                return getattr(out, key)
        raise TypeError(f"unexpected output {type(out)}")

    @torch.inference_mode()
    def img_embed(paths: list[Path]):
        ims = [Image.open(p).convert("RGB") for p in paths]
        inp = proc(images=ims, return_tensors="pt")
        return F.normalize(as_tensor(model.get_image_features(**inp)), dim=-1)

    @torch.inference_mode()
    def txt_embed(strings: list[str]):
        inp = proc(
            text=strings, padding="max_length", max_length=64, truncation=True, return_tensors="pt"
        )
        return F.normalize(as_tensor(model.get_text_features(**inp)), dim=-1)

    labels = list(texts["topics"].keys())
    try:
        topic_mat = torch.stack(
            [
                F.normalize(
                    txt_embed([SIGLIP_TEMPLATE.format(d.lower()) for d in texts["topics"][k]]).mean(
                        0
                    ),
                    dim=0,
                )
                for k in labels
            ]
        )
    except Exception as e:
        r["error"] = f"text tower: {type(e).__name__}: {e}"
        return r
    r["dim"] = int(topic_mat.shape[1])

    paths = [frame_dir / f["file"] for f in frames]
    img_embed(paths[:1])  # warm-up
    per_img = []
    singles = []
    for p in paths:
        t0 = time.perf_counter()
        singles.append(img_embed([p]))
        per_img.append(ms(t0))
    r["latencyPerImageBatch1Ms"] = summary(per_img)
    reel_times = []
    for i in range(0, min(len(paths), 25), 5):
        t0 = time.perf_counter()
        img_embed(paths[i : i + 5])
        reel_times.append(ms(t0))
    r["latency5FramesBatchMs"] = summary(reel_times)
    allv = torch.cat(singles)
    again = torch.cat([img_embed([p]) for p in paths[:5]])
    r["determinism"] = {"repeatMaxAbsDiff": float((allv[:5] - again).abs().max())}

    idx = [i for i, f in enumerate(frames) if f["topics"]]
    if idx:
        scores = allv[idx] @ topic_mat.T
        hits = topk_hits(scores, labels, [frames[i]["topics"] for i in idx])
        r["sanityZeroShotPhotos"] = {k: v for k, v in hits.items() if k != "predicted"}
        r["predictions"] = [
            {"file": frames[i]["file"], "want": frames[i]["topics"], "got": p}
            for i, p in zip(idx, hits["predicted"])
        ]
    return r


# ---------------------------------------------------------------- OCR


def norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).lower().split())


def substring_cer(target: str, text: str) -> float:
    """Edit distance of target against the best-matching substring of text, / len(target)."""
    t, s = norm(target), norm(text)
    if not t:
        return 0.0
    prev = [0] * (len(s) + 1)  # free start anywhere in s
    for i in range(1, len(t) + 1):
        cur = [i] + [0] * len(s)
        for j in range(1, len(s) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (t[i - 1] != s[j - 1]))
        prev = cur
    return round(min(prev) / len(t), 3)  # free end anywhere in s


def bench_ocr(name: str, params: dict, frames: list[dict], frame_dir: Path) -> dict:
    from rapidocr import RapidOCR

    r: dict = {"config": name}
    try:
        t0 = time.perf_counter()
        engine = RapidOCR(params=params)
        r["loadMs"] = ms(t0)
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        return r
    engine(str(frame_dir / frames[0]["file"]))  # warm-up
    lat, rows = [], []
    for f in frames:
        t0 = time.perf_counter()
        out = engine(str(frame_dir / f["file"]))
        lat.append(ms(t0))
        txts = list(out.txts or [])
        scores = [float(x) for x in (out.scores or [])]
        joined = " ".join(txts)
        rows.append(
            {
                "file": f["file"],
                "lang": f["overlayLang"],
                "cer": substring_cer(f["overlay"], joined),
                "meanScore": round(statistics.mean(scores), 3) if scores else None,
                "ocr": joined[:120],
            }
        )
    r["latencyPerFrameMs"] = summary(lat)
    by_lang: dict = {}
    for row in rows:
        by_lang.setdefault(row["lang"], []).append(row["cer"])
    r["cerByLang"] = {
        k: {
            "n": len(v),
            "median": round(statistics.median(v), 3),
            "recovered(cer<=0.1)": sum(1 for c in v if c <= 0.1),
        }
        for k, v in by_lang.items()
    }
    r["rows"] = rows
    return r


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--frames", type=Path, default=DEFAULT_FRAMES)
    ap.add_argument("--skip", nargs="*", default=[], choices=["e5", "siglip", "ocr"])
    a = ap.parse_args()

    os.environ["OMP_NUM_THREADS"] = str(a.threads)
    import psutil
    import torch

    torch.set_num_threads(a.threads)
    torch.manual_seed(0)

    texts = json.loads((HERE / "samples" / "texts.json").read_text(encoding="utf-8"))
    frames = json.loads((a.frames / "manifest.json").read_text(encoding="utf-8"))
    proc = psutil.Process()

    res: dict = {
        "startedAt": datetime.now(UTC).isoformat(),
        "threads": a.threads,
        "machine": {
            "cpu": platform.processor(),
            "logicalCpus": os.cpu_count(),
            "ramGB": round(psutil.virtual_memory().total / 2**30, 1),
            "python": platform.python_version(),
            "torch": torch.__version__,
        },
        "framesDir": str(a.frames),
        "nFrames": len(frames),
    }
    import transformers

    res["machine"]["transformers"] = transformers.__version__

    if "e5" not in a.skip:
        res["e5"] = bench_e5(texts, a.threads)
        res["rssAfterE5MB"] = round(proc.memory_info().rss / 2**20)
    if "siglip" not in a.skip:
        res["siglip"] = [bench_siglip(m, texts, frames, a.frames) for m in SIGLIP_IDS]
        res["rssAfterSiglipMB"] = round(proc.memory_info().rss / 2**20)
    if "ocr" not in a.skip:
        from rapidocr import LangRec, ModelType, OCRVersion

        base = {
            "EngineConfig.onnxruntime.intra_op_num_threads": a.threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.log_level": "error",
        }
        res["ocr"] = [
            bench_ocr("default (det+rec PP-OCRv6 small, ch/en)", dict(base), frames, a.frames),
            bench_ocr(
                "devanagari rec (PP-OCRv5 mobile)",
                {
                    **base,
                    "Rec.lang_type": LangRec.DEVANAGARI,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE,
                },
                frames,
                a.frames,
            ),
        ]
        res["rssAfterOcrMB"] = round(proc.memory_info().rss / 2**20)

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"bench-{a.threads}t-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()

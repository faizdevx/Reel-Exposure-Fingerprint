"""M0 throwaway: summarise an exported probe log (and optionally split frames).

Usage (stdlib only, any Python 3.10+):
    python -I summarize_probe.py <log.json> [--frames <frames.json> --out <dir>]

Prints a JSON summary used to fill docs/m0-feasibility-report.md.
Input files contain third-party Reel content: keep them in spikes/data/ (gitignored).
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
from collections import Counter
from pathlib import Path


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p / 100 * len(s)))], 1)


def dist(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "max": round(max(values), 1) if values else None,
    }


def final_code(e: dict) -> str | None:
    # Last code seen while this video was primary. urlCodeAtEnd is NOT used:
    # by the time the exposure ends the URL may already show the next Reel.
    if e.get("urlCodes"):
        return e["urlCodes"][-1]["code"]
    return e.get("urlCodeAtStart")


def summarise(doc: dict) -> dict:
    log = doc.get("log", [])
    exps = [e for e in log if e.get("type") == "exposure"]
    gt = doc.get("groundTruth")
    out: dict = {
        "logEntries": len(log),
        "exposures": len(exps),
        "groundTruth": gt,
        "userAgent": doc.get("userAgent"),
    }

    # Short "exposures" (<300 ms primary) are likely scroll-through flicker.
    real = [e for e in exps if e.get("primaryMs", 0) >= 300]
    out["exposuresOver300ms"] = len(real)
    out["flickerUnder300ms"] = len(exps) - len(real)

    # F4 / E1: detection and duplicates.
    codes = [final_code(e) for e in real]
    out["missingCode"] = sum(1 for c in codes if not c)
    consecutive_dupes = sum(1 for a, b in zip(codes, codes[1:]) if a and a == b)
    out["consecutiveSameCode"] = consecutive_dupes
    out["uniqueCodes"] = len({c for c in codes if c})
    out["repeatCodesNonConsecutive"] = (
        sum(v - 1 for v in Counter(c for c in codes if c).values()) - consecutive_dupes
    )
    if gt:
        out["detectionRateVsGroundTruth"] = round(out["uniqueCodes"] / gt, 3)

    # URL shortcode behaviour.
    changed = [e for e in real if e.get("urlCodes")]
    out["url"] = {
        "exposuresWithUrlChangeWhilePrimary": len(changed),
        "urlChangeLagMs": dist([e["urlCodes"][0]["lagMs"] for e in changed]),
        "urlCodeChangedBeforePrimary": sum(
            1 for e in real if not e.get("urlCodes") and e.get("urlCodeAtStart")
        ),
        "multipleUrlChangesWhilePrimary": sum(1 for e in real if len(e.get("urlCodes", [])) > 1),
    }
    agree = [e for e in real if (e.get("metaEnd") or {}).get("domCodes")]
    out["url"]["domCodeAvailable"] = len(agree)
    out["url"]["domCodeMatchesUrl"] = sum(
        1 for e in agree if final_code(e) in e["metaEnd"]["domCodes"]
    )
    out["url"]["domCodeMatchesUrlAtEnd"] = sum(
        1 for e in agree if e.get("urlCodeAtEnd") in e["metaEnd"]["domCodes"]
    )
    out["url"]["urlAtEndDiffersFromFinal"] = sum(
        1 for e in real if e.get("urlCodeAtEnd") and e.get("urlCodeAtEnd") != final_code(e)
    )

    # E1 / N1: primary detection.
    out["primaryDetectLatencyMs"] = dist(
        [e["detectLatencyMs"] for e in real if e.get("detectLatencyMs") is not None]
    )
    out["primaryCause"] = Counter(e.get("cause") for e in real)
    out["endReason"] = Counter(e.get("reason") for e in exps)

    # E3: metadata.
    def field_rate(key: str) -> dict:
        vals = [(e.get("metaEnd") or {}) for e in real]
        return {"available": sum(1 for m in vals if m.get(key)), "of": len(vals)}

    metas = [(e.get("metaEnd") or {}) for e in real]
    out["meta"] = {
        "handle": field_rate("handle"),
        "handleMatchesAltText": sum(
            1 for m in metas if m.get("handle") and m.get("handle") == m.get("altHandle")
        ),
        "altHandleAvailable": sum(1 for m in metas if m.get("altHandle")),
        "multipleHandleCandidates": sum(
            1 for m in metas if len(m.get("handleCandidates") or []) > 1
        ),
        "caption": field_rate("caption"),
        "captionStrategy": Counter(m.get("captionStrategy") for m in metas),
        "captionTruncatedMarker": sum(1 for m in metas if m.get("truncated")),
        "captionChangedDuringExposure": sum(1 for e in real if e.get("metaChanged")),
        "tagLinksAvailable": sum(1 for m in metas if m.get("tagLinks")),
        "regexTagsAvailable": sum(1 for m in metas if m.get("regexTags")),
        "tagsAgree": sum(
            1
            for m in metas
            if m.get("regexTags") and set(m.get("tagLinks") or []) >= set(m["regexTags"])
        ),
        "containerDepth": dist([m["containerDepth"] for m in metas if "containerDepth" in m]),
        "extractMs": dist([m["ms"] for m in metas if "ms" in m]),
        "errors": sum(1 for m in metas if m.get("error")),
    }

    # E6 (spec items 8-10): playing, visibility, dwell.
    out["dwell"] = {
        "dwellMs": dist([e["dwellMs"] for e in real]),
        "primaryMs": dist([e["primaryMs"] for e in real]),
        "exposuresWithPausedTime": sum(1 for e in real if e.get("pausedMs", 0) > 0),
        "exposuresWithHiddenTime": sum(1 for e in real if e.get("hiddenMs", 0) > 0),
        "dwellExceedsPrimary": sum(1 for e in real if e["dwellMs"] > e["primaryMs"] + 300),
        "accountingGapMs": dist(
            [
                abs(e["primaryMs"] - e["dwellMs"] - e.get("pausedMs", 0) - e.get("hiddenMs", 0))
                for e in real
            ]
        ),
        "videoDurationAvailable": sum(1 for e in real if isinstance(e.get("videoDurationMs"), int)),
        "loopAttr": Counter(str(e.get("loop")) for e in real),
        "visibilityEvents": Counter(e.get("state") for e in log if e.get("type") == "visibility"),
    }

    # E2: frames.
    fa = [f for e in real for f in e.get("frames", []) if f.get("method") == "A"]
    fb = [f for e in real for f in e.get("frames", []) if f.get("method") == "B"]
    out["frameA"] = {
        "attempts": len(fa),
        "ok": sum(1 for f in fa if f.get("ok")),
        "errors": Counter(f.get("error") for f in fa if not f.get("ok")),
        "bytes": dist([f["bytes"] for f in fa if f.get("bytes")]),
        "drawMs": dist([f["drawMs"] for f in fa if "drawMs" in f]),
        "encodeMs": dist([f["encodeMs"] for f in fa if "encodeMs" in f]),
        "blackFrames": sum(
            1 for f in fa if f.get("luma") and f["luma"]["mean"] < 8 and f["luma"]["std"] < 3
        ),
        "canvasToBlob": Counter(
            "ok" if f["canvasToBlob"].get("ok") else f["canvasToBlob"].get("error")
            for f in fa
            if f.get("canvasToBlob")
        ),
        "srcResolution": Counter(f"{f.get('srcW')}x{f.get('srcH')}" for f in fa if f.get("srcW")),
    }
    per_reel = [
        sum(
            f.get("bytes", 0) for f in e.get("frames", []) if f.get("method") == "A" and f.get("ok")
        )
        for e in real
    ]
    per_reel = [b for b in per_reel if b]
    out["frameA"]["bytesPerReel"] = dist(per_reel)
    out["frameA"]["medianKBPerReel"] = (
        round(statistics.median(per_reel) / 1024, 1) if per_reel else None
    )
    out["frameB"] = {
        "attempts": len(fb),
        "ok": sum(1 for f in fb if f.get("ok")),
        "errors": Counter(f.get("error") for f in fb if not f.get("ok")),
        "bytes": dist([f["bytes"] for f in fb if f.get("bytes")]),
        "captureMs": dist([f["captureMs"] for f in fb if "captureMs" in f]),
        "cropMs": dist([f["cropMs"] for f in fb if "cropMs" in f]),
    }
    out["capturePopup"] = [e for e in log if e.get("type") == "capture_popup"]
    out["captureRate"] = [e.get("results") for e in log if e.get("type") == "capture_rate"]

    # N2: overhead.
    perfs = [e for e in log if e.get("type") == "perf"]

    def perf_key(k: str, stat: str) -> list[float]:
        return [p[k][stat] for p in perfs if p.get(k) and p[k].get(stat) is not None]

    out["perf"] = {
        k: {
            "p95_of_windows_max": max(perf_key(k, "p95"), default=None),
            "max": max(perf_key(k, "max"), default=None),
            "n": sum(p[k]["n"] for p in perfs if p.get(k)),
        }
        for k in ("mutation", "io", "drawA", "encodeA", "meta")
    }
    out["perf"]["pageLongtasks"] = sum(p.get("longtasks", 0) for p in perfs)

    # Surfaces seen (A-2).
    out["urlSurfaces"] = Counter(
        f"{e.get('from')}->{e.get('to')}" for e in log if e.get("type") == "url"
    )
    out["contentScriptLoadedOn"] = Counter(
        e.get("surface") for e in log if e.get("type") == "cs_loaded"
    )
    return out


def split_frames(path: Path, out_dir: Path) -> int:
    doc = json.loads(path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, f in enumerate(doc.get("frames", [])):
        header, _, b64 = f["dataUrl"].partition(",")
        if "image/jpeg" not in header:
            continue
        name = f"{i:04d}_{f.get('method')}_{f.get('code') or 'nocode'}_{f.get('offsetS')}s.jpg"
        (out_dir / name).write_bytes(base64.b64decode(b64))
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("--frames", type=Path)
    ap.add_argument("--out", type=Path, default=Path("../data/frames"))
    a = ap.parse_args()
    summary = summarise(json.loads(a.log.read_text(encoding="utf-8")))
    if a.frames:
        summary["framesWritten"] = split_frames(a.frames, a.out)
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()

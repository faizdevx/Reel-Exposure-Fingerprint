"""Command line: `python -m app.evaluation embed|evaluate` (run from `server/`).

    # 1. Run the real models once per labelled Reel and keep the cosines (slow, needs `--group ml`):
    python -m app.evaluation embed --labels ../labels/labels.jsonl --out ../labels/cosines.npz \\
        [--visual-model siglip2-base|siglip-multilingual] [--model-cache DIR]

    # 2. Compare text only, visual only and fused, and tune (fast, no model):
    python -m app.evaluation evaluate --cache ../labels/cosines.npz \\
        --labels ../labels/labels.jsonl --out ../labels/report.json [--seed 0]

The report is JSON. It records its inputs (hash of the labelled set, pipeline versions, seed) and
all results. Below the minimum data it contains metrics and intervals and recommends nothing.
Labelled sets built from real Reels stay out of git.
"""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from sqlalchemy.orm import Session as DbSession

from app.db import get_engine
from app.evaluation.collect import collect, load_labelling_report
from app.evaluation.cosines import (
    CacheError,
    build_cosines,
    file_sha256,
    load_cosines,
    save_cosines,
)
from app.evaluation.labels import LabelFileError, label_counts, load_labels
from app.evaluation.render import render
from app.evaluation.tuning import evaluate
from app.fingerprint.engine import FingerprintConfig
from app.prediction.config import PredictionConfig
from app.taxonomy import load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.embedder import E5Embedder
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import VISION_MODELS, VisualPipelineConfig
from app.worker.visual_embedder import SigLipEmbedder
from app.worker.visual_pipeline import VisualPipeline

REPO_ROOT = Path(__file__).resolve().parents[3]


def _embed(args: argparse.Namespace) -> int:
    taxonomy = load_taxonomy(args.taxonomy)
    reels = load_labels(args.labels, taxonomy)
    print(f"{len(reels)} labelled Reels; labels: {label_counts(reels)}")

    text_config = TextPipelineConfig()
    text = TextPipeline.create(
        text_config,
        taxonomy,
        E5Embedder(text_config.model, cache_dir=args.model_cache),
        args.embedding_cache,
    )
    visual: VisualPipeline | None = None
    visual_meta: dict[str, object] | None = None
    if args.visual_model:
        model = replace(VISION_MODELS[args.visual_model], threads=args.threads)
        config = VisualPipelineConfig(model)
        visual = VisualPipeline.create(
            config,
            taxonomy,
            SigLipEmbedder(model, cache_dir=args.model_cache),
            args.embedding_cache,
        )
        visual_meta = {
            "key": model.key,
            "model_id": model.model_id,
            "revision": model.revision,
            "license": model.license,
        }
    data = build_cosines(
        reels, text, visual, labels_sha256=file_sha256(args.labels), visual_meta=visual_meta
    )
    save_cosines(data, args.out)
    print(f"wrote {args.out}")
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    sha = file_sha256(args.labels) if args.labels else None
    data = load_cosines(args.cache, labels_sha256=sha)
    report = evaluate(data, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    rec = report["recommendation"]
    print(f"wrote {args.out}")
    print(f"recommendation: {rec['status']}")
    for reason in rec["reasons"]:
        print(f"  - {reason}")
    if rec["settings"] is not None:
        print(f"  settings: {rec['settings']}")
    return 0


def _report(args: argparse.Namespace) -> int:
    labelling = load_labelling_report(args.labelling_report) if args.labelling_report else None
    with DbSession(get_engine()) as db:
        report = collect(
            db,
            assignment_version=args.assignment_version,
            labelling=labelling,
            labelling_file=args.labelling_report,
            fp_config=FingerprintConfig(),
            prediction_config=PredictionConfig(),
            seed=args.seed,
        )
    if args.note:
        report["note"] = args.note
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report), encoding="utf-8")
    print(f"wrote {args.out}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.json}")
    for name in ("labelling", "self_report", "prediction", "stability"):
        print(f"  {name}: {report[name]['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)

    embed = sub.add_parser("embed", help="run the models once and keep the cosines")
    embed.add_argument("--labels", type=Path, required=True)
    embed.add_argument("--out", type=Path, required=True)
    embed.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "taxonomy" / "topics.json")
    embed.add_argument("--visual-model", choices=sorted(VISION_MODELS), default=None)
    embed.add_argument("--threads", type=int, default=4)
    embed.add_argument("--model-cache", type=Path, default=None)
    embed.add_argument("--embedding-cache", type=Path, default=None)
    embed.set_defaults(run=_embed)

    ev = sub.add_parser("evaluate", help="compare modalities and tune from the cache")
    ev.add_argument("--cache", type=Path, required=True)
    ev.add_argument("--labels", type=Path, default=None, help="checked against the cache")
    ev.add_argument("--out", type=Path, required=True)
    ev.add_argument("--seed", type=int, default=0)
    ev.set_defaults(run=_evaluate)

    rep = sub.add_parser("report", help="the four evaluations of spec §10, into a report")
    rep.add_argument("--assignment-version", required=True)
    rep.add_argument("--labelling-report", type=Path, default=None)
    rep.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evaluation-report.md")
    rep.add_argument("--json", type=Path, default=None)
    rep.add_argument("--seed", type=int, default=0)
    rep.add_argument("--note", default=None, help="a line printed under the title")
    rep.set_defaults(run=_report)

    args = parser.parse_args(argv)
    try:
        return int(args.run(args))
    except (LabelFileError, CacheError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

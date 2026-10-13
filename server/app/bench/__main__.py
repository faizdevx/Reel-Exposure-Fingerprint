"""`python -m app.bench` (run from `server/`).

    python -m app.bench models   --out ../docs/performance/models.json
    python -m app.bench pipeline --out ../docs/performance/pipeline.json --database-url ...bench
    python -m app.bench api      --out ../docs/performance/api.json      --database-url ...bench
    python -m app.bench report                                           # docs/performance-report.md

`models` and `pipeline` need `uv sync --group ml --group bench` and the models in the Hugging Face
cache (`--model-cache`; `--allow-download` lets them be fetched). `pipeline` and `api` need a
PostgreSQL database whose name ends in `_bench`: they wipe it. The browser half is
`npm run bench` in `extension/`. Everything is seeded and recorded with the machine description.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.bench import environment

REPO = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO / "docs" / "performance"


def _write(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {path}")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--threads", type=int, default=4, help="torch and onnxruntime threads (spec: 4 vCPU)"
    )
    p.add_argument("--cpus", type=int, default=4, help="pin to this many CPUs; 0 = do not pin")
    p.add_argument("--model-cache", type=Path, default=None, help="Hugging Face cache directory")
    p.add_argument("--allow-download", action="store_true", help="let the models be downloaded")
    p.add_argument("--taxonomy", type=Path, default=REPO / "taxonomy" / "topics.json")
    p.add_argument(
        "--frames", type=Path, default=None, help="JPEG folder instead of generated frames"
    )


def _setup(args: argparse.Namespace) -> dict[str, Any]:
    pinned = environment.pin_cpus(args.cpus) if args.cpus else None
    env = environment.collect(cpus=args.cpus or None, threads=args.threads)
    env["pinned_cpu_ids"] = pinned
    return env


def _models(args: argparse.Namespace) -> int:
    from app.bench.suites import models

    env = _setup(args)
    offline = not args.allow_download
    result: dict[str, Any] = {"suite": "models", "environment": env, "seed": 0}
    result["text_embedding"] = models.text_embedding(
        n=args.n,
        threads=args.threads,
        taxonomy_file=args.taxonomy,
        model_cache=args.model_cache,
        offline=offline,
    )
    result["visual_embedding"] = {}
    for key in args.visual:
        result["visual_embedding"][key] = models.visual_embedding(
            key=key,
            n_frames=args.frames_n,
            threads=args.threads,
            taxonomy_file=args.taxonomy,
            model_cache=args.model_cache,
            offline=offline,
            frames_dir=args.frames,
        )
    result["ocr"] = {}
    for kind in args.ocr:
        result["ocr"][kind] = models.ocr(
            kind=kind, n_frames=args.frames_n, threads=args.threads, frames_dir=args.frames
        )
    _write(args.out, result)
    return 0


def _database_url(args: argparse.Namespace) -> str:
    url = args.database_url or os.environ.get("REF_BENCH_DATABASE_URL")
    if not url:
        raise SystemExit(
            "error: give --database-url or set REF_BENCH_DATABASE_URL (a *_bench database)"
        )
    return str(url)


def _pipeline(args: argparse.Namespace) -> int:
    from app.bench.suites import pipeline

    env = _setup(args)
    job = pipeline.PipelineJob(
        database_url=_database_url(args),
        taxonomy_file=args.taxonomy,
        model_cache=args.model_cache,
        offline=not args.allow_download,
        visual=None if args.visual == "none" else args.visual,
        ocr=None if args.ocr == "none" else args.ocr,
        frames_per_reel=args.frames_per_reel,
        ocr_frames=args.ocr_frames,
        threads=args.threads,
        frames_dir=args.frames,
        ort_spin=not args.ort_no_spin,
    )
    result = pipeline.run(job=job, reels=args.reels, workers=args.workers, cpus=args.cpus or None)
    _write(args.out, {"suite": "pipeline", "environment": env, "seed": 0, "result": result})
    return 0


def _api(args: argparse.Namespace) -> int:
    import psutil

    from app.bench.suites import api

    url = _database_url(args)
    server_cpus, client_cpus = environment.cpu_plan(args.cpus) if args.cpus else (None, None)
    if client_cpus:
        psutil.Process().cpu_affinity(client_cpus)
    env = environment.collect(cpus=args.cpus or None, threads=args.threads)
    env["server_cpu_ids"], env["client_cpu_ids"] = server_cpus, client_cpus
    env["database"] = api.database_description(url)
    result: dict[str, Any] = {"suite": "api", "environment": env, "seed": 0}
    if not args.skip_seed:
        result["seeded_database"] = api.seed(
            url, users=args.users, reels_per_user=args.reels_per_user, taxonomy_file=args.taxonomy
        )
    seeded = result.get("seeded_database") or {"users": args.users}
    result["dashboard_api"] = api.dashboard(
        seeded, url, n=args.requests, cpus=server_cpus, port=args.port, clients=args.clients
    )
    result["event_upload"] = api.ingest(url, cpus=server_cpus, port=args.port, reps=args.reps)
    _write(args.out, result)
    return 0


def _contention(args: argparse.Namespace) -> int:
    from app.bench.suites import contention

    env = _setup(args)
    result = contention.run(
        n=args.n,
        threads=args.threads,
        model_cache=args.model_cache,
        offline=not args.allow_download,
        visual=args.visual,
    )
    _write(args.out, {"suite": "contention", "environment": env, "seed": 0, **result})
    return 0


def _report(args: argparse.Namespace) -> int:
    from app.bench import report

    text = report.render(report.load(args.dir))
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("models", help="text, visual and OCR latency")
    _common(m)
    m.add_argument("--n", type=int, default=100, help="captions to time")
    m.add_argument("--frames-n", type=int, default=40, help="frames to time per model")
    m.add_argument("--visual", nargs="*", default=["siglip2-base", "siglip-multilingual"])
    m.add_argument("--ocr", nargs="*", default=["default", "devanagari"])
    m.set_defaults(run=_models)

    pl = sub.add_parser("pipeline", help="the worker on N Reels end to end (N3)")
    _common(pl)
    pl.add_argument(
        "--database-url", default=None, help="a PostgreSQL database named *_bench (WIPED)"
    )
    pl.add_argument("--reels", type=int, default=200)
    pl.add_argument("--workers", type=int, default=1)
    pl.add_argument(
        "--visual", default="siglip2-base", choices=["siglip2-base", "siglip-multilingual", "none"]
    )
    pl.add_argument("--ocr", default="devanagari", choices=["default", "devanagari", "none"])
    pl.add_argument("--frames-per-reel", type=int, default=5)
    pl.add_argument("--ocr-frames", type=int, default=2)
    pl.add_argument(
        "--ort-no-spin", action="store_true", help="ONNX Runtime threads sleep when idle"
    )
    pl.set_defaults(run=_pipeline)

    ap = sub.add_parser(
        "api", help="dashboard API p95 (N4) and event upload latency, at N11 capacity"
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--database-url", default=None, help="a PostgreSQL database named *_bench (WIPED)"
    )
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument(
        "--cpus", type=int, default=4, help="CPUs for the API; the load generator gets the rest"
    )
    ap.add_argument("--taxonomy", type=Path, default=REPO / "taxonomy" / "topics.json")
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--reels-per-user", type=int, default=20_000)
    ap.add_argument("--requests", type=int, default=300, help="requests per endpoint and client")
    ap.add_argument(
        "--clients", type=int, default=None, help="logins at once (default: one per user)"
    )
    ap.add_argument("--reps", type=int, default=60, help="uploads per batch size")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--skip-seed", action="store_true", help="measure the database as it is")
    ap.set_defaults(run=_api)

    ct = sub.add_parser("contention", help="does OCR or the image model slow the text model?")
    _common(ct)
    ct.add_argument("--n", type=int, default=40)
    ct.add_argument(
        "--visual", default="siglip2-base", choices=["siglip2-base", "siglip-multilingual"]
    )
    ct.set_defaults(run=_contention)

    rp = sub.add_parser("report", help="docs/performance-report.md from docs/performance/*.json")
    rp.add_argument("--dir", type=Path, default=DEFAULT_OUT)
    rp.add_argument("--out", type=Path, default=REPO / "docs" / "performance-report.md")
    rp.set_defaults(run=_report)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())

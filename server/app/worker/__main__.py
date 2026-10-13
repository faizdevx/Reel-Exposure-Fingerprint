"""Run the worker: `python -m app.worker [--once]`."""

import argparse
import logging

from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db import get_engine
from app.worker.factory import build_reel_pipeline
from app.worker.loop import run_forever


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.worker")
    parser.add_argument("--once", action="store_true", help="Drain the queue, rebuild, exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(
        level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    pipeline = build_reel_pipeline()
    logging.getLogger("app.worker").info("worker start pipeline_version=%s", pipeline.version)
    with DbSession(get_engine()) as db:
        run_forever(db, pipeline, poll_seconds=args.poll_seconds, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

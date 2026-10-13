"""FastAPI application: liveness, auth, tokens, consent, sessions, ingest, fingerprint reads."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.body_limit import BodyLimitMiddleware
from app.config import get_settings
from app.frames import MAX_UPLOAD_BODY_BYTES, FrameStore
from app.ratelimit import Limits, charge_ip
from app.routers import (
    auth,
    consent,
    events,
    extension_tokens,
    fingerprint,
    frames,
    me,
    predictions,
    self_report,
    sessions,
    topics,
)
from app.routers.events import HARD_BODY_CEILING as EVENTS_BODY_CEILING

DEFAULT_BODY_BYTES = 16 * 1024  # ADR-013: every other endpoint


class Health(BaseModel):
    status: str


log = logging.getLogger("app.main")


async def _sweep_frames_forever(interval: int) -> None:
    """The 24 hour backstop of F15: expired frames are removed even if no worker ran."""
    settings = get_settings()
    store = FrameStore(settings.frame_store_dir)
    max_age = settings.frame_ttl_hours * 3600
    while True:
        try:
            await asyncio.to_thread(store.sweep, max_age)
        except Exception:  # a failed sweep must not stop the next one, or the API
            log.exception("frame sweep failed")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    interval = get_settings().frame_sweep_interval_seconds
    task = asyncio.create_task(_sweep_frames_forever(interval)) if interval > 0 else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)
    app = FastAPI(title="Reel Exposure Fingerprint", version="0.1.0", lifespan=lifespan)
    app.state.limits = Limits.from_settings(get_settings())
    # Every route under /api/ is bounded (audit H-2, ADR-013): 16 KB unless a rule gives it more.
    app.add_middleware(
        BodyLimitMiddleware,
        rules=[
            (r"/api/v1/events/[^/]+/frames", MAX_UPLOAD_BODY_BYTES),
            (r"/api/v1/events", EVENTS_BODY_CEILING),
        ],
        default_limit=DEFAULT_BODY_BYTES,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_failed(_request: Request, error: RequestValidationError) -> JSONResponse:
        """Locations and error types only. FastAPI's default body repeats the submitted values,
        which would send passwords and invite codes back (and a large body back in full)."""
        detail = [{"loc": list(e["loc"]), "type": e["type"]} for e in error.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.get("/healthz", response_model=Health, tags=["ops"])
    def healthz() -> Health:
        """Liveness. Deliberately independent of the database."""
        return Health(status="ok")

    # Every /api/ request is counted against the caller's address before anything else runs.
    api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(charge_ip)])
    api_v1.include_router(auth.router)
    api_v1.include_router(extension_tokens.router)
    api_v1.include_router(consent.router)
    api_v1.include_router(sessions.router)
    api_v1.include_router(events.router)
    api_v1.include_router(frames.router)
    api_v1.include_router(fingerprint.router)
    api_v1.include_router(topics.router)
    api_v1.include_router(me.router)
    api_v1.include_router(predictions.router)
    api_v1.include_router(self_report.router)
    app.include_router(api_v1)
    return app


app = create_app()

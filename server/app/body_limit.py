"""Request body size cap at the ASGI layer, before any parsing (ADR-013, N6, audit H-2).

A rule is a path pattern and a byte limit; the first rule that matches the whole path applies. A
path under `/api/` that no rule matches gets `default_limit`, so no route is left unbounded by
forgetting to list it. A request whose `Content-Length` is over the limit is answered 413 without
reading the body. A request with no length (chunked) is counted as it streams, and abandoned with
413 the moment it passes the limit, so a hostile upload costs at most `limit` bytes of memory.

The 413 is decided here, not by the route. Frameworks catch exceptions while parsing a body and
turn them into their own error (FastAPI answers 400 for a JSON body that cannot be read), so the
middleware remembers that the limit was passed and replaces whatever response the application then
starts with 413. A request that went over its limit can never be answered with anything else.
"""

import contextlib
import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_BODY = json.dumps({"detail": "payload_too_large"}).encode()


class BodyTooLarge(Exception):
    """Raised from `receive` once the body passes the limit; the middleware turns it into 413.

    Code that reads the body should let it through, but need not: the 413 does not depend on it.
    """


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        rules: list[tuple[str, int]],
        *,
        default_limit: int | None = None,
        default_prefix: str = "/api/",
    ) -> None:
        self.app = app
        self.rules = [(re.compile(pattern), limit) for pattern, limit in rules]
        self.default_limit = default_limit
        self.default_prefix = default_prefix

    def _limit_for(self, path: str) -> int | None:
        for pattern, limit in self.rules:
            if pattern.fullmatch(path):
                return limit
        if self.default_limit is not None and path.startswith(self.default_prefix):
            return self.default_limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope.get("path", "")) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False  # the app's own parser rejects a malformed length
            if too_big:
                await self._reject(send)
                return

        seen = 0
        exceeded = False
        answered = False  # our 413 has been sent

        async def counting_receive() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal answered
            if answered:
                return  # the application is still talking; the client has its 413
            if exceeded:
                answered = True
                await self._reject(send)
                return
            await send(message)

        with contextlib.suppress(BodyTooLarge):
            await self.app(scope, counting_receive, guarded_send)
        if exceeded and not answered:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_BODY)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _BODY})

"""Rate limiting (spec N6, ADR-005, security audit H-1).

Token buckets held in the memory of the API process. With one API process and at most five users
that is enough (ADR-005); the limits reset when the process restarts. If the API ever runs more
than one process the counters must move to the database.

What is limited, and by what key:

- every authenticated request: per user and request class (`ingest`, `token`, `dashboard`,
  `destructive`), charged where the credential is checked (`app.auth_deps`), so a route cannot be
  added without it. Ingest (`POST /events` and the frame upload) is 60 a minute with a burst of 20
  (ADR-005). A user's limit does not depend on how many extension tokens they hold: minting more
  tokens buys nothing.
- every `/api/` request: per client address, so an unauthenticated flood is counted too.
- `/auth/login` and `/auth/register`: per client address, and failed logins per email address (5
  failures, then one more attempt every 3 minutes), checked before any password work is done.
- password hashing: at most `password_concurrency` verifications or hashes at the same time
  (each costs 64 MiB); more are answered 503 with `Retry-After`, never queued.

A refused request is `429 {"detail": "rate_limited"}` with `Retry-After` in whole seconds (503
`busy` for the password cap). The extension keeps its queue and waits that long.

Memory is bounded: a bucket table holds at most `max_keys` keys and drops the least recently used,
so a flood of random keys cannot grow it without limit (an evicted key simply starts full again).
"""

import contextlib
import hashlib
import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import Settings

Clock = Callable[[], float]
# Refilling in floating point can land a hair under a whole token at exactly the promised time.
_EPSILON = 1e-9


class RateLimiter:
    """Token buckets by key. `check` takes one token and says how long to wait if there is none."""

    def __init__(
        self,
        *,
        burst: int,
        per_minute: float,
        clock: Clock = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if burst < 1 or per_minute <= 0:
            raise ValueError("burst must be at least 1 and the rate positive")
        self.burst = burst
        self.rate = per_minute / 60.0  # tokens per second
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _refilled(self, key: str, now: float) -> float:
        tokens, updated = self._buckets.get(key, (float(self.burst), now))
        return min(float(self.burst), tokens + max(0.0, now - updated) * self.rate)

    def _store(self, key: str, tokens: float, now: float) -> None:
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)

    def check(self, key: str, cost: float = 1.0) -> float:
        """0.0 when allowed (the token is taken); otherwise seconds until it would be."""
        with self._lock:
            now = self._clock()
            tokens = self._refilled(key, now)
            if tokens + _EPSILON >= cost:
                self._store(key, max(0.0, tokens - cost), now)
                return 0.0
            self._store(key, tokens, now)
            return (cost - tokens) / self.rate

    def peek(self, key: str) -> float:
        """Like `check` but takes nothing: seconds until one token is available (0.0 if now)."""
        with self._lock:
            tokens = self._refilled(key, self._clock())
            return 0.0 if tokens + _EPSILON >= 1.0 else (1.0 - tokens) / self.rate

    def spend(self, key: str) -> None:
        """Take a token whether or not there is one (a failure that must count)."""
        with self._lock:
            now = self._clock()
            self._store(key, max(0.0, self._refilled(key, now) - 1.0), now)

    def reset_key(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()

    def __len__(self) -> int:
        return len(self._buckets)


def too_many(retry_after: float, detail: str = "rate_limited", status: int = 429) -> HTTPException:
    seconds = max(1, math.ceil(retry_after))
    return HTTPException(status_code=status, detail=detail, headers={"Retry-After": str(seconds)})


@dataclass
class Limits:
    """All limiters of one application."""

    ingest: RateLimiter
    token: RateLimiter
    dashboard: RateLimiter
    destructive: RateLimiter
    api_ip: RateLimiter
    auth_ip: RateLimiter
    login_failures: RateLimiter
    password_slots: threading.BoundedSemaphore
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @classmethod
    def from_settings(cls, s: Settings, clock: Clock = time.monotonic) -> "Limits":
        def make(burst: int, per_minute: float) -> RateLimiter:
            return RateLimiter(
                burst=burst, per_minute=per_minute, clock=clock, max_keys=s.rate_limit_max_keys
            )

        networks = tuple(
            ipaddress.ip_network(part.strip(), strict=False)
            for part in s.trusted_proxies.split(",")
            if part.strip()
        )
        return cls(
            ingest=make(s.ingest_burst, s.ingest_per_minute),
            token=make(s.token_burst, s.token_per_minute),
            dashboard=make(s.dashboard_burst, s.dashboard_per_minute),
            destructive=make(s.destructive_burst, s.destructive_per_minute),
            api_ip=make(s.api_ip_burst, s.api_ip_per_minute),
            auth_ip=make(s.auth_ip_burst, s.auth_ip_per_minute),
            login_failures=make(s.login_failures_per_email, 60.0 / s.login_failure_refill_seconds),
            password_slots=threading.BoundedSemaphore(s.password_concurrency),
            trusted_proxies=networks,
        )

    def clear(self) -> None:
        for limiter in (
            self.ingest,
            self.token,
            self.dashboard,
            self.destructive,
            self.api_ip,
            self.auth_ip,
            self.login_failures,
        ):
            limiter.clear()


def limits_of(request: Request) -> Limits:
    limits: Limits = request.app.state.limits
    return limits


# ------------------------------------------------------------------ who is calling


def client_ip(request: Request, limits: Limits | None = None) -> str:
    """The caller's address. `X-Forwarded-For` counts only when the direct peer is a configured
    trusted proxy, and then the right-most entry that is not itself a trusted proxy is the client:
    anything a client put to the left of that is ignored, so it cannot choose its own key."""
    limits = limits or limits_of(request)
    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not _trusted(peer_address, limits):
        return peer
    forwarded = [
        p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()
    ]
    for entry in reversed(forwarded):
        try:
            address = ipaddress.ip_address(entry)
        except ValueError:
            return peer  # a malformed chain is not believed
        if not _trusted(address, limits):
            return str(address)
    return peer


def _trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, limits: Limits) -> bool:
    return any(address in network for network in limits.trusted_proxies)


# ------------------------------------------------------------------ the checks the routes use


def request_class(request: Request, via: str) -> str:
    """Which per-user limiter a request is charged to. `via` is the credential that was accepted
    ("token" or "cookie"), not a header the caller may have added."""
    path = request.url.path
    if request.method == "POST" and path.startswith("/api/v1/events"):
        return "ingest:frames" if path.endswith("/frames") else "ingest:events"
    if request.method == "DELETE":
        return "destructive"
    return "token" if via == "token" else "dashboard"


def charge_user(request: Request, user_id: object, via: str) -> None:
    """Called where a credential is accepted. Raises 429 when the user is over their limit."""
    limits = limits_of(request)
    kind = request_class(request, via)
    if kind.startswith("ingest"):
        limiter, key = limits.ingest, f"{kind}:{user_id}"
    elif kind == "destructive":
        limiter, key = limits.destructive, f"destructive:{user_id}"
    elif kind == "token":
        limiter, key = limits.token, f"token:{user_id}"
    else:
        limiter, key = limits.dashboard, f"dashboard:{user_id}"
    wait = limiter.check(key)
    if wait > 0.0:
        raise too_many(wait)


def charge_ip(request: Request) -> None:
    """Router-level guard for every /api/ request, before any credential is looked at."""
    limits = limits_of(request)
    wait = limits.api_ip.check(f"ip:{client_ip(request, limits)}")
    if wait > 0.0:
        raise too_many(wait)


def charge_auth_attempt(request: Request) -> None:
    limits = limits_of(request)
    wait = limits.auth_ip.check(f"auth:{client_ip(request, limits)}")
    if wait > 0.0:
        raise too_many(wait)


def email_key(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def login_locked(request: Request, email: str) -> None:
    """Refuse before any password work when this email has used up its failures."""
    wait = limits_of(request).login_failures.peek(email_key(email))
    if wait > 0.0:
        raise too_many(wait)


def login_failed(request: Request, email: str) -> None:
    limits_of(request).login_failures.spend(email_key(email))


def login_succeeded(request: Request, email: str) -> None:
    limits_of(request).login_failures.reset_key(email_key(email))


@contextlib.contextmanager
def password_work(request: Request) -> Iterator[None]:
    """At most `password_concurrency` hashes or verifications at once; the rest get 503."""
    slots = limits_of(request).password_slots
    if not slots.acquire(blocking=False):
        raise too_many(1.0, detail="busy", status=503)
    try:
        yield
    finally:
        slots.release()

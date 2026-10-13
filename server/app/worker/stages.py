"""Stage runner: each stage reports when it started and finished, how long it took, how it ended.

A stage ends in one of three ways:

- `ok`: it returned a value;
- `skipped`: it had nothing to do or could not run, for a stated reason (`reason` is a code such as
  `no_text` or `upstream_embed_text`); this is not an error;
- `failed`: it raised. `error` is the exception CLASS NAME. The exception message is dropped on
  purpose: messages can quote user text (a caption inside a validation error, say), and reports and
  logs must carry IDs and error classes only (CONTRIBUTING.md).

`BaseException` that is not an `Exception` (KeyboardInterrupt, SystemExit) is not caught.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

log = logging.getLogger("app.worker")

StageStatus = Literal["ok", "failed", "skipped"]


class StageSkipped(Exception):
    """Raised inside a stage to skip it. `reason` is a short code, never user text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Clocks:
    """Wall clock for timestamps, monotonic clock for durations (never subtract wall times)."""

    wall: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic: Callable[[], float] = field(default=time.monotonic)


SYSTEM_CLOCKS = Clocks()


@dataclass(frozen=True)
class StageReport:
    stage: str
    status: StageStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    #: Exception class name when `status` is `failed`, else None.
    error: str | None = None
    #: Reason code when `status` is `skipped`, else None.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Outcome[T]:
    report: StageReport
    value: T | None = None

    @property
    def ok(self) -> bool:
        return self.report.status == "ok"


class StageRunner:
    """Runs stages one after another and collects their reports in order."""

    def __init__(self, clocks: Clocks = SYSTEM_CLOCKS) -> None:
        self._clocks = clocks
        self.reports: list[StageReport] = []

    def run[T](self, stage: str, fn: Callable[[], T]) -> Outcome[T]:
        started_at = self._clocks.wall()
        t0 = self._clocks.monotonic()
        status: StageStatus = "ok"
        error: str | None = None
        reason: str | None = None
        value: T | None = None
        try:
            value = fn()
        except StageSkipped as skip:
            status, reason = "skipped", skip.reason
        except Exception as exc:  # a stage failing must not stop the others
            status, error = "failed", type(exc).__name__
        duration_ms = max(0.0, (self._clocks.monotonic() - t0) * 1000.0)
        return self._record(
            Outcome(
                StageReport(
                    stage=stage,
                    status=status,
                    started_at=started_at,
                    finished_at=self._clocks.wall(),
                    duration_ms=duration_ms,
                    error=error,
                    reason=reason,
                ),
                value,
            )
        )

    def skip(self, stage: str, reason: str) -> Outcome[Any]:
        """Record a stage that was not attempted, with no work and no duration."""
        now = self._clocks.wall()
        report = StageReport(stage, "skipped", now, now, 0.0, reason=reason)
        return self._record(Outcome(report))

    def _record[T](self, outcome: Outcome[T]) -> Outcome[T]:
        report = outcome.report
        self.reports.append(report)
        # Stage name, status, duration and error class only: no content.
        log.info(
            "stage=%s status=%s duration_ms=%.1f error=%s reason=%s",
            report.stage,
            report.status,
            report.duration_ms,
            report.error,
            report.reason,
        )
        return outcome

    def status_of(self, stage: str) -> StageStatus | None:
        for report in reversed(self.reports):
            if report.stage == stage:
                return report.status
        return None

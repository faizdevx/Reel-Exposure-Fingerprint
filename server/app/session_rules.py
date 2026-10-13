"""Monitoring-session rules shared by the consent and session endpoints (spec F2, ADR-014).

The 60 minute cap and the 60 second off-surface rule are spec values, not settings. The extension
applies both; the server enforces the cap too, so a client that never stops cannot keep a session
open past it.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import Session as MonitoringSession

MAX_SESSION = timedelta(minutes=60)

# Reasons a client may report. consent_revoked is set by the server alone.
CLIENT_STOP_REASONS = ("user", "left_surface", "tab_closed", "time_cap", "browser_restart")


def cap_time(session: MonitoringSession) -> datetime:
    return session.started_at + MAX_SESSION


def stop(session: MonitoringSession, *, now: datetime, reason: str) -> None:
    """Stop an active session. A session past its cap ends at the cap with reason time_cap."""
    if session.status != "active":
        return
    if now >= cap_time(session):
        session.ended_at = cap_time(session)
        session.stop_reason = "time_cap"
    else:
        session.ended_at = now
        session.stop_reason = reason
    session.status = "stopped"


def active_sessions(db: DbSession, user_id: uuid.UUID) -> list[MonitoringSession]:
    return list(
        db.execute(
            select(MonitoringSession).where(
                MonitoringSession.user_id == user_id, MonitoringSession.status == "active"
            )
        ).scalars()
    )


def close_previous(db: DbSession, user_id: uuid.UUID, *, now: datetime) -> None:
    """Make room for a new session: at most one is active per user (DB-enforced).

    Overdue sessions end at their cap (time_cap). Any other leftover is a client that never
    reported a stop, typically a crashed browser (browser_restart).
    """
    for session in active_sessions(db, user_id):
        stop(session, now=now, reason="browser_restart")

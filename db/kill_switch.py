"""Kill switch state, backed by KillSwitchLog rather than in-memory state.

api/main.py (the dashboard process) and functions/market_hours_trading.py
(the trading loop, a separate process per Section 8's Azure Functions
architecture) both need to see the same kill switch state — an in-memory
flag in one process is invisible to the other.
"""

from .session import SessionLocal
from .models import KillSwitchLog, NotificationOutbox


def is_bot_enabled() -> bool:
    db = SessionLocal()
    try:
        latest = db.query(KillSwitchLog).order_by(KillSwitchLog.id.desc()).first()
        return latest.enabled if latest else True
    finally:
        db.close()


def set_bot_enabled(enabled: bool, reason: str) -> None:
    db = SessionLocal()
    try:
        db.add(KillSwitchLog(enabled=enabled, reason=reason))
        if not enabled:
            db.add(NotificationOutbox(body=f"Trading Bot ALERT: new entries paused ({reason}). Position protection continues."))
        db.commit()
    finally:
        db.close()

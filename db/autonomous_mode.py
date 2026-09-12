"""Autonomous-trading toggle state, backed by AutonomousModeLog — mirrors
db/kill_switch.py's pattern so both the dashboard process and the trading
loop see the same state regardless of which process flips it.

Deliberately defaults to OFF (no row = disabled): the kill switch is an
emergency stop that defaults to "bot allowed to trade," but autonomous
mode is a separate, opt-in decision the user makes explicitly from the
dashboard — building the capability shouldn't silently turn it on.
"""

from .session import SessionLocal
from .models import AutonomousModeLog


def is_autonomous_enabled() -> bool:
    db = SessionLocal()
    try:
        latest = db.query(AutonomousModeLog).order_by(AutonomousModeLog.id.desc()).first()
        return latest.enabled if latest else False
    finally:
        db.close()


def set_autonomous_enabled(enabled: bool, reason: str) -> None:
    db = SessionLocal()
    try:
        db.add(AutonomousModeLog(enabled=enabled, reason=reason))
        db.commit()
    finally:
        db.close()

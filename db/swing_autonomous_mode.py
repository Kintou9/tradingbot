"""
Autonomous-execution toggle for the experimental swing strategy —
mirrors db/autonomous_mode.py exactly, but as its own table/module so it
can never be conflated with (or flipped together with) the live
strategy's autonomous-trading switch. Also defaults to OFF when no row
exists.
"""

from .session import SessionLocal
from .models import SwingAutonomousModeLog


def is_swing_autonomous_enabled() -> bool:
    db = SessionLocal()
    try:
        latest = db.query(SwingAutonomousModeLog).order_by(SwingAutonomousModeLog.id.desc()).first()
        return latest.enabled if latest else False
    finally:
        db.close()


def get_swing_autonomous_variant(default: str = "A") -> str:
    db = SessionLocal()
    try:
        latest = db.query(SwingAutonomousModeLog).order_by(SwingAutonomousModeLog.id.desc()).first()
        return latest.variant if latest and latest.variant else default
    finally:
        db.close()


def set_swing_autonomous_enabled(enabled: bool, variant: str, reason: str) -> None:
    db = SessionLocal()
    try:
        db.add(SwingAutonomousModeLog(enabled=enabled, variant=variant, reason=reason))
        db.commit()
    finally:
        db.close()

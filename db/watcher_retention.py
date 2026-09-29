"""Expire discovery candidates after 14 elapsed days (timestamps stored in UTC)."""
from datetime import datetime, timedelta, timezone

from .models import WatchedTicker
from .session import SessionLocal

WATCHER_RETENTION_DAYS = 14
AUTO_DISCOVERY_PREFIX = "Auto-discovered "


def expires_at(watched):
    # This is the marker already written by discovery, including historic rows.
    # Manual additions have no automatic retention deadline.
    if not (watched.notes or "").startswith(AUTO_DISCOVERY_PREFIX):
        return None
    return watched.added_at + timedelta(days=WATCHER_RETENTION_DAYS)


def expire_watched(db, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=WATCHER_RETENTION_DAYS)
    expired = (db.query(WatchedTicker)
        .filter(WatchedTicker.added_at <= cutoff,
                WatchedTicker.notes.startswith(AUTO_DISCOVERY_PREFIX, autoescape=True))
        .order_by(WatchedTicker.added_at, WatchedTicker.id).all())
    tickers = [row.ticker for row in expired]
    if expired:
        # Delete only selected memberships, never holdings, trades or research.
        db.query(WatchedTicker).filter(WatchedTicker.id.in_([row.id for row in expired])).delete(synchronize_session=False)
        db.commit()
        db.expire_all()
    return tickers


def run_watcher_cleanup():
    db = SessionLocal()
    try:
        return expire_watched(db)
    finally:
        db.close()

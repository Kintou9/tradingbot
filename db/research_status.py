"""Shared freshness and retry rules for the research queue and Watcher UI."""
from datetime import datetime, timedelta, timezone
import json
import os

from .models import ResearchNote, ResearchStatus

MODULES = ("deep_dive", "technical_scan", "dcf")
MAX_AGE = timedelta(hours=24)
RETRY_DELAY = timedelta(hours=6)
RUNNING_TIMEOUT = timedelta(hours=1)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def latest_notes(db, ticker):
    return {module: db.query(ResearchNote).filter(
        ResearchNote.ticker == ticker, ResearchNote.module == module,
        ResearchNote.structured_output.isnot(None)
    ).order_by(ResearchNote.created_at.desc(), ResearchNote.id.desc()).first() for module in MODULES}


def research_state(db, ticker, now=None):
    now = now or utcnow()
    notes = latest_notes(db, ticker)
    attempt = db.get(ResearchStatus, ticker)
    complete = all(notes.values())
    fresh = (complete and all(note.created_at > now - MAX_AGE for note in notes.values())
             and not (attempt and attempt.status in {"failed", "partial"}))
    errors = json.loads(attempt.errors_json or "[]") if attempt else []
    retry_at = None
    if attempt and attempt.status == "running" and attempt.started_at > now - RUNNING_TIMEOUT:
        status = "running"
        due = False
    elif attempt and attempt.status in {"failed", "partial"} and attempt.finished_at and attempt.finished_at > now - RETRY_DELAY:
        status = attempt.status
        retry_at = attempt.finished_at + RETRY_DELAY
        due = False
    else:
        status = "current" if fresh else ("stale" if any(notes.values()) else "queued")
        due = not fresh
    if os.getenv("RESEARCH_SCHEDULE_ENABLED", "true").lower() != "true" and due:
        status = "paused"
    deep = notes["deep_dive"]
    return {"status": status, "due": due, "errors": errors,
            "assessment_updated_at": deep.created_at.isoformat() + "Z" if deep else None,
            "assessment_stale": bool(deep and deep.created_at <= now - MAX_AGE),
            "last_attempt_at": attempt.started_at.isoformat() + "Z" if attempt else None,
            "retry_at": retry_at.isoformat() + "Z" if retry_at else None}

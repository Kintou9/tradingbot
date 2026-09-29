"""Bounded research queue, independent of trading switches and market hours."""
import os

from db.execution_lock import execution_lock, ExecutionBusy
from db.research_status import research_state, utcnow
from db.session import SessionLocal
from functions.after_hours_research import research_targets, run_after_hours_research

RESEARCH_BATCH_SIZE = 2


def run_scheduled_research():
    if os.getenv("RESEARCH_SCHEDULE_ENABLED", "true").lower() != "true":
        return []
    db = SessionLocal()
    try:
        # Separate from the execution lock so research cannot stall protection.
        with execution_lock(db, lock_id=731908413):
            now = utcnow()
            states = [(ticker, research_state(db, ticker, now)) for ticker in research_targets(db)]
            due = [(ticker, state) for ticker, state in states if state["due"]]
            # Never-researched first, then the oldest attempted, to avoid a
            # repeatedly failing provider starving later Watcher candidates.
            due.sort(key=lambda item: item[1]["last_attempt_at"] or "")
            return run_after_hours_research(tickers=[ticker for ticker, _ in due[:RESEARCH_BATCH_SIZE]]) if due else []
    except ExecutionBusy:
        return []
    finally:
        db.close()

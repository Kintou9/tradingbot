"""
Position supervision and market-hours entries. Protection and reconciliation
run even with entries paused; cached research is required only for entries.
"""

import json
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from engine.entry_exit.signals import evaluate_entry, evaluate_exit
from broker.alpaca_client import client as alpaca_client, get_account, get_positions
from db.session import SessionLocal
from db.models import ResearchNote, NewsItem, Position, RuntimeState
from db.kill_switch import is_bot_enabled, set_bot_enabled
from db.autonomous_mode import is_autonomous_enabled
from db.execution_lock import execution_lock
from engine.entry_exit.execution import execute_buy, execute_sell, maintain_protection
from engine.entry_exit.risk import Limits, RiskRejected, loss_breached

load_dotenv()

WATCHLIST = ["PLTR", "AVGO", "MSFT", "NOW", "CRWD", "AVPT", "DDOG", "SNOW", "CLFD", "AGYS"]

MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.1"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.02"))

# How stale cached research is allowed to be before we refuse to trade on it.
RESEARCH_MAX_AGE_HOURS = 24
NEWS_LOOKBACK_HOURS = 24


def is_market_hours() -> bool:
    """Delegates to Alpaca's own market calendar, which already accounts
    for weekends, holidays, and early closes — no need for a separate
    pandas_market_calendars dependency."""
    return alpaca_client.get_clock().is_open


def _latest_research(db, ticker: str, module: str) -> dict | None:
    cutoff = datetime.utcnow() - timedelta(hours=RESEARCH_MAX_AGE_HOURS)
    note = (
        db.query(ResearchNote)
        .filter(
            ResearchNote.ticker == ticker,
            ResearchNote.module == module,
            ResearchNote.created_at >= cutoff,
        )
        .order_by(ResearchNote.created_at.desc())
        .first()
    )
    if note is None or not note.structured_output:
        return None
    return json.loads(note.structured_output)


def _avg_sentiment(db, ticker: str) -> float:
    cutoff = datetime.utcnow() - timedelta(hours=NEWS_LOOKBACK_HOURS)
    items = (
        db.query(NewsItem)
        .filter(NewsItem.ticker == ticker, NewsItem.ingested_at >= cutoff)
        .all()
    )
    scores = [i.sentiment_score for i in items if i.sentiment_score is not None]
    return sum(scores) / len(scores) if scores else 0.0


def _daily_loss_limit_breached(account) -> bool:
    return loss_breached(account, Limits.from_env())


def run_market_hours_trading():
    """Protection always runs. Switches and daily loss only gate NEW entries."""
    db = SessionLocal()
    try:
        with execution_lock(db):
            return _run_trading_cycle(db)
    finally:
        db.close()


def _run_trading_cycle(db):
    issues = maintain_protection(db)
    account = get_account()
    if _daily_loss_limit_breached(account) and is_bot_enabled():
        set_bot_enabled(False, reason="daily_loss_limit_hit")
        # Immediately cancel pending entries after tripping the loss switch.
        issues.extend(maintain_protection(db))
    if not is_market_hours():
        return {"issues": issues, "market_open": False}

    held_positions = {p.symbol: p for p in get_positions()}
    tracked = list(db.query(Position).all())
    held_this_cycle = set(held_positions)
    # All tracked holdings, even ones removed from the watchlist. No research
    # dependency here: yesterday's research cannot disable today's exits.
    for position_row in tracked:
        ticker = position_row.ticker
        held = held_positions.get(ticker)
        if held is None:
            continue
        try:
            exit_signal = evaluate_exit(ticker, entry_price=float(held.avg_entry_price),
                current_price=float(held.current_price), stop_loss_price=position_row.stop_loss_price,
                take_profit_price=position_row.take_profit_price)
            if exit_signal:
                execute_sell(db, ticker, min(float(held.qty), position_row.quantity), exit_signal.reason)
        except Exception as exc:
            db.rollback()
            issues.append(f"{ticker}: exit deferred ({exc})")

    if not is_autonomous_enabled() or not is_bot_enabled() or issues:
        return {"issues": issues, "market_open": True}
    # Persist the entry cadence so multiple workers/restarts do not run a fresh
    # watchlist evaluation on every protection tick.
    cadence = db.get(RuntimeState, "primary_entry_cycle")
    now = datetime.utcnow()
    if cadence and (now - cadence.updated_at).total_seconds() < 300:
        return {"issues": issues, "market_open": True}
    if cadence is None:
        cadence = RuntimeState(key="primary_entry_cycle")
        db.add(cadence)
    cadence.value, cadence.updated_at = now.isoformat(), now
    db.commit()
    for ticker in WATCHLIST:
        if ticker in held_this_cycle:
            continue
        try:
            technical_result = _latest_research(db, ticker, "technical_scan")
            valuation_result = _latest_research(db, ticker, "dcf")
            if technical_result is None or valuation_result is None:
                issues.append(f"{ticker}: fresh technical/valuation research missing; entry skipped")
                continue
            entry_signal = evaluate_entry(ticker, valuation_result, technical_result,
                _avg_sentiment(db, ticker), max_position_pct=Limits.from_env().max_position_pct,
                account_equity=float(account.equity))
            if entry_signal:
                execute_buy(db, ticker, reason=entry_signal.reason,
                    stop_loss_price=technical_result.get("stop_loss"),
                    take_profit_price=technical_result.get("target_1"))
        except RiskRejected as exc:
            db.rollback()
            print(f"{ticker}: entry skipped ({exc})")
        except Exception as exc:
            db.rollback()
            issues.append(f"{ticker}: entry failed ({exc})")
    return {"issues": issues, "market_open": True}


if __name__ == "__main__":
    print(run_market_hours_trading())

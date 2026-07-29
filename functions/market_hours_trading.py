"""
Market-hours trading loop — intended as an Azure Function with a Timer
trigger, running only during market hours (Section 8). Checks the kill
switch, evaluates entry/exit signals against cached research, and places
orders through the broker client.
"""

import json
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from engine.entry_exit.signals import evaluate_entry, evaluate_exit
from broker.alpaca_client import client as alpaca_client, get_account, get_positions, place_market_order
# from broker.robinhood_client import get_positions, place_market_order  # alt. if using Robinhood
from db.session import SessionLocal
from db.models import ResearchNote, NewsItem, Position, Trade
from db.kill_switch import is_bot_enabled, set_bot_enabled

load_dotenv()

WATCHLIST = ["PLTR", "AVGO", "MSFT", "NOW", "CRWD", "AVPT", "DDOG", "SNOW", "CLFD", "AGYS"]

MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.1"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))

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
    """Section 7: max daily loss limit that halts the bot automatically."""
    last_equity = float(account.last_equity)
    if last_equity <= 0:
        return False
    daily_pl_pct = (float(account.equity) - last_equity) / last_equity
    return daily_pl_pct <= -DAILY_LOSS_LIMIT_PCT


def run_market_hours_trading():
    if not is_bot_enabled():
        print("Kill switch engaged, skipping trading cycle.")
        return

    if not is_market_hours():
        print("Outside market hours, skipping.")
        return

    account = get_account()
    if _daily_loss_limit_breached(account):
        set_bot_enabled(False, reason="daily_loss_limit_hit")
        print("Daily loss limit breached — kill switch engaged, skipping trading cycle.")
        return

    account_equity = float(account.equity)
    held_positions = {p.symbol: p for p in get_positions()}

    db = SessionLocal()
    try:
        for ticker in WATCHLIST:
            technical_result = _latest_research(db, ticker, "technical_scan")
            if technical_result is None:
                continue

            held = held_positions.get(ticker)
            if held is not None:
                position_row = db.query(Position).filter(Position.ticker == ticker).first()
                if position_row is None:
                    # Position exists at the broker but isn't one this bot
                    # opened/is tracking (e.g. opened manually) — leave it alone.
                    continue

                exit_signal = evaluate_exit(
                    ticker,
                    entry_price=float(held.avg_entry_price),
                    current_price=float(held.current_price),
                    stop_loss_price=position_row.stop_loss_price,
                    take_profit_price=position_row.take_profit_price,
                )
                if exit_signal:
                    place_market_order(ticker, qty=float(held.qty), side="sell")
                    db.add(Trade(
                        ticker=ticker,
                        action="sell",
                        quantity=float(held.qty),
                        price=float(held.current_price),
                        reason=exit_signal.reason,
                    ))
                    db.delete(position_row)
                    db.commit()
                    print(f"{ticker}: SELL submitted ({exit_signal.reason})")
                continue

            valuation_result = _latest_research(db, ticker, "dcf")
            if valuation_result is None:
                continue

            sentiment_score = _avg_sentiment(db, ticker)
            entry_signal = evaluate_entry(
                ticker,
                valuation_result,
                technical_result,
                sentiment_score,
                max_position_pct=MAX_POSITION_PCT,
                account_equity=account_equity,
            )
            if entry_signal:
                entry_price = technical_result["entry_price"]
                qty = round((account_equity * MAX_POSITION_PCT) / entry_price, 4)
                place_market_order(ticker, qty=qty, side="buy")
                db.add(Trade(
                    ticker=ticker,
                    action="buy",
                    quantity=qty,
                    price=entry_price,
                    reason=entry_signal.reason,
                ))
                db.add(Position(
                    ticker=ticker,
                    quantity=qty,
                    avg_entry_price=entry_price,
                    stop_loss_price=technical_result.get("stop_loss"),
                    take_profit_price=technical_result.get("target_1"),
                ))
                db.commit()
                print(f"{ticker}: BUY submitted ({entry_signal.reason}, qty={qty})")

        print("Market-hours trading cycle complete.")
    finally:
        db.close()


if __name__ == "__main__":
    run_market_hours_trading()

"""
Forward paper-trading runner for the experimental swing strategy
(engine.swing_strategy). Deliberately its own script, invoked manually —
NOT wired into api/main.py's autonomous-trading loop, NOT reachable from
the dashboard's manual buy/sell buttons, and NOT the live strategy in
functions/market_hours_trading.py. This is the safety boundary requested:
"do not enable the new strategy for live execution."

Two modes:
    --dry-run (default): evaluates the watchlist, logs every candidate
        (accepted or rejected) to SwingCandidateLog, updates any open
        SwingPosition's status, but places NO orders. This is how you
        "collect prospectively through paper trading" (point 6) the two
        variants (C, D) that can't be backtested historically, without
        risking a single dollar — not even paper dollars.
    --execute: does everything --dry-run does, and additionally submits
        real Alpaca PAPER orders (this project has never traded anything
        but paper — see broker/alpaca_client.py's PAPER flag) for
        accepted candidates, and records fills into SwingTrade/
        SwingPosition. Still entirely isolated from the live Trade/
        Position tables and the live account's actual paper positions
        are never touched by this file.

Usage:
    python -m functions.swing_paper_trading --dry-run
    python -m functions.swing_paper_trading --execute --variant A
"""

import argparse
import json
from datetime import date, datetime, timezone

from broker.alpaca_client import client as alpaca_client, get_account, get_positions, place_market_order
from db.session import SessionLocal
from db.models import NewsItem, ResearchNote, SwingCandidateLog, SwingPosition, SwingTrade
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.swing_strategy.technical_trigger import find_pullback_setup, check_confirmation_intraday, PullbackSetup
from engine.swing_strategy.engine import evaluate_swing_candidate
from engine.swing_strategy.exits import evaluate_swing_exit_live
from engine.swing_strategy.trading_calendar import sessions_held_live
from functions.market_hours_trading import WATCHLIST, RESEARCH_MAX_AGE_HOURS, NEWS_LOOKBACK_HOURS
from datetime import timedelta


def _latest_research(db, ticker: str, module: str) -> tuple[dict | None, datetime | None]:
    cutoff = datetime.utcnow() - timedelta(hours=RESEARCH_MAX_AGE_HOURS)
    note = (
        db.query(ResearchNote)
        .filter(ResearchNote.ticker == ticker, ResearchNote.module == module, ResearchNote.created_at >= cutoff)
        .order_by(ResearchNote.created_at.desc())
        .first()
    )
    if note is None or not note.structured_output:
        return None, None
    return json.loads(note.structured_output), note.created_at


def _recent_sentiment_scores(db, ticker: str) -> tuple[list[float], datetime | None]:
    cutoff = datetime.utcnow() - timedelta(hours=NEWS_LOOKBACK_HOURS)
    items = db.query(NewsItem).filter(NewsItem.ticker == ticker, NewsItem.ingested_at >= cutoff).all()
    scores = [i.sentiment_score for i in items if i.sentiment_score is not None]
    latest_ts = max((i.ingested_at for i in items), default=None)
    return scores, latest_ts


def _log_candidate(db, candidate) -> None:
    db.add(SwingCandidateLog(
        ticker=candidate.ticker, variant=candidate.variant, strategy_version=candidate.strategy_version,
        config_json=None, accepted=candidate.accepted, rejection_reason=candidate.rejection_reason,
        signal_timestamp=candidate.signal_timestamp,
        source_data_timestamps_json=json.dumps({k: (v.isoformat() if v else None) for k, v in candidate.source_data_timestamps.items()}),
        entry_price=candidate.entry_price, stop_price=candidate.stop_price, target_price=candidate.target_price,
        reward_to_risk=candidate.reward_to_risk, shares=candidate.shares,
        planned_dollar_risk=candidate.planned_dollar_risk, estimated_cost=candidate.estimated_cost,
        dcf_fair_value=candidate.dcf_fair_value, dcf_upside=candidate.dcf_upside,
        dcf_discount_to_fair_value=candidate.dcf_discount_to_fair_value, dcf_share_count_source=candidate.dcf_share_count_source,
        sentiment_avg=candidate.sentiment_avg, sentiment_article_count=candidate.sentiment_article_count,
        llm_confidence=candidate.llm_confidence,
    ))
    db.commit()


def _open_risk_dollars(db, account_equity: float) -> float:
    open_positions = db.query(SwingPosition).filter(SwingPosition.status == "open").all()
    return sum(p.quantity * (p.entry_price - p.stop_price) for p in open_positions)


def run_swing_paper_cycle(config: SwingStrategyConfig, execute: bool = False) -> dict:
    account = get_account()
    account_equity = float(account.equity)
    cash_available = float(account.cash)
    buying_power = float(account.buying_power)
    held_by_broker = {p.symbol for p in get_positions()}

    db = SessionLocal()
    summary = {"evaluated": 0, "accepted": [], "rejected": [], "exits": []}
    try:
        open_positions = {p.ticker: p for p in db.query(SwingPosition).filter(SwingPosition.status == "open").all()}

        # --- Exits first: never look for a new entry on a ticker we're
        # already holding via this strategy in the same cycle. ---
        for ticker, pos in open_positions.items():
            try:
                current_price = float(fetch_ohlcv(ticker, outputsize=1)["close"].iloc[-1])
            except Exception as exc:
                summary["exits"].append({"ticker": ticker, "error": f"price unavailable: {exc}"})
                continue
            sessions_held = sessions_held_live(alpaca_client, pos.entry_session_date, date.today())
            decision = evaluate_swing_exit_live(
                current_price=current_price, stop_price=pos.stop_price, target_price=pos.target_price,
                sessions_held=sessions_held, max_holding_trading_days=config.max_holding_trading_days,
                duplicate_exit_pending=pos.exit_order_pending,
            )
            if not decision.should_exit:
                continue
            summary["exits"].append({"ticker": ticker, "reason": decision.reason.value, "exit_price": decision.exit_price})
            if execute:
                pos.exit_order_pending = True
                db.commit()
                order = place_market_order(ticker, qty=pos.quantity, side="sell")
                db.add(SwingTrade(ticker=ticker, action="sell", quantity=pos.quantity, price=decision.exit_price,
                                   reason=decision.reason.value, order_id=str(order.id)))
                pos.status = "closed"
                pos.exit_reason = decision.reason.value
                pos.exit_price = decision.exit_price
                pos.exit_timestamp = datetime.now(timezone.utc)
                pos.sessions_held_at_exit = sessions_held
                pos.realized_pl = (decision.exit_price - pos.entry_price) * pos.quantity
                db.commit()

        open_risk = _open_risk_dollars(db, account_equity)

        # --- Entries ---
        for ticker in WATCHLIST:
            if ticker in open_positions or ticker in held_by_broker:
                continue
            summary["evaluated"] += 1
            try:
                df = fetch_ohlcv(ticker, outputsize=250)
            except Exception as exc:
                summary["rejected"].append({"ticker": ticker, "reason": f"OHLCV fetch failed: {exc}"})
                continue
            df = compute_indicators(df)
            df = df.join(rolling_support_resistance(df, window=config.support_resistance_window))

            # setup/confirmation may end up None below — evaluate_swing_candidate
            # handles both cases (and logs the resulting rejection), so every
            # ticker that got this far gets a SwingCandidateLog row, not just
            # ones that reached a confirmed setup.
            setup: PullbackSetup | None = find_pullback_setup(df, len(df) - 2, config)
            confirmation = None
            if setup is not None:
                # Revalidate with a fresh quote right now — research/setup
                # freshness (yesterday's close) does not establish quote
                # freshness for an intraday confirmation decision.
                try:
                    latest_price = float(fetch_ohlcv(ticker, outputsize=1)["close"].iloc[-1])
                    session_open = float(df.iloc[-1]["open"])
                    confirmation = check_confirmation_intraday(setup, session_open, latest_price, as_of=datetime.now(timezone.utc))
                except Exception as exc:
                    summary["rejected"].append({"ticker": ticker, "reason": f"live quote unavailable: {exc}"})
                    continue

            dcf_structured, dcf_ts = (None, None)
            sentiment_scores, sentiment_ts = ([], None)
            if config.gate_variant in (GateVariant.TECHNICAL_PLUS_DCF, GateVariant.TECHNICAL_PLUS_BOTH):
                dcf_structured, dcf_ts = _latest_research(db, ticker, "dcf")
            if config.gate_variant in (GateVariant.TECHNICAL_PLUS_SENTIMENT, GateVariant.TECHNICAL_PLUS_BOTH):
                sentiment_scores, sentiment_ts = _recent_sentiment_scores(db, ticker)

            candidate = evaluate_swing_candidate(
                ticker=ticker, setup=setup, confirmation=confirmation, config=config,
                account_equity=account_equity, cash_available=cash_available,
                open_risk_dollars=open_risk, buying_power=buying_power,
                source_data_timestamps={"ohlcv": df.index[-1].to_pydatetime(), "dcf": dcf_ts, "sentiment": sentiment_ts},
                dcf_structured=dcf_structured, sentiment_scores=sentiment_scores,
            )
            _log_candidate(db, candidate)

            if not candidate.accepted:
                summary["rejected"].append({"ticker": ticker, "reason": candidate.rejection_reason})
                continue

            summary["accepted"].append({
                "ticker": ticker, "entry_price": candidate.entry_price, "stop_price": candidate.stop_price,
                "target_price": candidate.target_price, "reward_to_risk": candidate.reward_to_risk,
                "shares": candidate.shares, "planned_dollar_risk": candidate.planned_dollar_risk,
            })
            open_risk += candidate.planned_dollar_risk  # so a later ticker this same cycle sees the updated total

            if execute:
                order = place_market_order(ticker, qty=candidate.shares, side="buy")
                db.add(SwingTrade(ticker=ticker, action="buy", quantity=candidate.shares, price=candidate.entry_price,
                                   estimated_cost=candidate.estimated_cost, reason="swing_entry", order_id=str(order.id)))
                db.add(SwingPosition(
                    ticker=ticker, quantity=candidate.shares, entry_price=candidate.entry_price,
                    stop_price=candidate.stop_price, target_price=candidate.target_price,
                    entry_timestamp=datetime.now(timezone.utc), entry_session_date=date.today(),
                    strategy_version=config.strategy_version, config_json=json.dumps(config.as_dict()),
                    status="open",
                ))
                db.commit()

        return summary
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="place real Alpaca PAPER orders (default: log-only dry run)")
    parser.add_argument("--variant", choices=["A", "B", "C", "D"], default="A", help="gate variant to run (default: A)")
    args = parser.parse_args()

    cfg = SwingStrategyConfig(gate_variant=GateVariant(args.variant))
    result = run_swing_paper_cycle(cfg, execute=args.execute)
    print(json.dumps(result, indent=2, default=str))

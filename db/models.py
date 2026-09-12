"""
SQLAlchemy models for trade history, research cache, and news items.
Matches Section 8 of the requirements doc (PostgreSQL: trade history,
P&L, ingested news items).
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Text, Boolean
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    action = Column(String)  # "buy" or "sell"
    quantity = Column(Float)
    price = Column(Float)
    timestamp = Column(DateTime, default=datetime.utcnow)
    reason = Column(String)  # "valuation_trigger", "stop_loss", "manual", etc.
    notes = Column(Text, nullable=True)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, unique=True, index=True)
    quantity = Column(Float)
    avg_entry_price = Column(Float)
    stop_loss_price = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow)


class ResearchNote(Base):
    """Cached output from the after-hours LLM analyst modules
    (Section 11: Ticker Deep Dive, DCF, Earnings Quality, Technical Scanner)."""
    __tablename__ = "research_notes"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    module = Column(String)  # "deep_dive", "dcf", "earnings_quality", "technical_scan"
    verdict = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    raw_output = Column(Text)  # full markdown/text response
    structured_output = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)


class NewsItem(Base):
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    headline = Column(String)
    source = Column(String, nullable=True)
    url = Column(String, nullable=True)
    sentiment_score = Column(Float, nullable=True)  # -1.0 to 1.0
    published_at = Column(DateTime, nullable=True)
    ingested_at = Column(DateTime, default=datetime.utcnow)


class WatchedTicker(Base):
    """Personal candidate list — tickers the user is considering but hasn't
    added to the bot's own automated WATCHLIST (functions/*.py) yet. Purely
    for tracking; adding a ticker here does not trigger research or trading."""
    __tablename__ = "watched_tickers"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, unique=True, index=True)
    notes = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class KillSwitchLog(Base):
    """Audit trail every time the bot is halted/resumed, and why."""
    __tablename__ = "kill_switch_log"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean)
    reason = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class DiscoveryRunLog(Base):
    """One row per weekly stock-discovery run (engine/discovery), so the
    scheduler can tell "already ran this week" from "still due," and the
    dashboard can show what showed up and when."""
    __tablename__ = "discovery_run_log"

    id = Column(Integer, primary_key=True)
    run_date = Column(Date)
    tickers = Column(Text, nullable=True)  # comma-separated, for a quick glance
    timestamp = Column(DateTime, default=datetime.utcnow)


class SwingCandidateLog(Base):
    """Every candidate the experimental swing strategy (engine.swing_strategy)
    evaluated, accepted or rejected — the audit trail point 6 of the
    2026-09-11 swing-strategy work asked for. Entirely separate from
    ResearchNote/Trade/Position, which the live strategy still owns."""
    __tablename__ = "swing_candidate_log"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    variant = Column(String)  # "A" | "B" | "C" | "D" — see engine.swing_strategy.config.GateVariant
    strategy_version = Column(String)
    config_json = Column(Text)

    accepted = Column(Boolean)
    rejection_reason = Column(String, nullable=True)

    signal_timestamp = Column(DateTime)
    source_data_timestamps_json = Column(Text, nullable=True)

    entry_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    reward_to_risk = Column(Float, nullable=True)

    shares = Column(Float, nullable=True)
    planned_dollar_risk = Column(Float, nullable=True)
    estimated_cost = Column(Float, nullable=True)

    dcf_fair_value = Column(Float, nullable=True)
    dcf_upside = Column(Float, nullable=True)
    dcf_discount_to_fair_value = Column(Float, nullable=True)
    dcf_share_count_source = Column(String, nullable=True)

    sentiment_avg = Column(Float, nullable=True)
    sentiment_article_count = Column(Integer, nullable=True)

    llm_confidence = Column(Float, nullable=True)  # informational only, never gates

    created_at = Column(DateTime, default=datetime.utcnow)


class SwingPosition(Base):
    """Open/closed positions for the experimental swing strategy — kept
    entirely separate from Position (the live strategy's table) so this
    system can never be confused with, or accidentally touch, real
    tracked positions like MSFT/AVPT."""
    __tablename__ = "swing_positions"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    quantity = Column(Float)
    entry_price = Column(Float)
    stop_price = Column(Float)
    target_price = Column(Float)

    entry_timestamp = Column(DateTime)
    entry_session_date = Column(Date)  # the trading session the entry filled in, for session counting
    strategy_version = Column(String)
    config_json = Column(Text)

    status = Column(String, default="open")  # "open" | "closed"
    exit_reason = Column(String, nullable=True)  # "stop_loss" | "take_profit" | "time_based_exit"
    exit_price = Column(Float, nullable=True)
    exit_timestamp = Column(DateTime, nullable=True)
    sessions_held_at_exit = Column(Integer, nullable=True)
    realized_pl = Column(Float, nullable=True)

    exit_order_pending = Column(Boolean, default=False)  # duplicate-exit-order guard


class SwingTrade(Base):
    """Actual simulated/paper fills for the swing strategy — mirrors
    Trade's shape but stays isolated from the live trades table."""
    __tablename__ = "swing_trades"

    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    action = Column(String)  # "buy" or "sell"
    quantity = Column(Float)
    price = Column(Float)
    estimated_cost = Column(Float, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    reason = Column(String)  # "swing_entry", "stop_loss", "take_profit", "time_based_exit"
    order_id = Column(String, nullable=True)


class AutonomousModeLog(Base):
    """Audit trail for the dashboard's autonomous-trading toggle. Separate
    from the kill switch (an emergency stop that defaults to *enabled*) —
    this defaults to *disabled* when no row exists, so the bot only trades
    unattended once the user explicitly turns it on from the dashboard."""
    __tablename__ = "autonomous_mode_log"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean)
    reason = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class SwingAutonomousModeLog(Base):
    """Same pattern as AutonomousModeLog, but for the experimental swing
    strategy (engine.swing_strategy) — a deliberately separate switch so
    it can never be confused with, or accidentally flipped together with,
    the live strategy's autonomous-trading toggle. Also defaults to
    disabled when no row exists."""
    __tablename__ = "swing_autonomous_mode_log"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean)
    variant = Column(String, nullable=True)  # "A" | "B" | "C" | "D" active when this row was written
    reason = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

"""
SQLAlchemy models for trade history, research cache, and news items.
Matches Section 8 of the requirements doc (PostgreSQL: trade history,
P&L, ingested news items).
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
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


class KillSwitchLog(Base):
    """Audit trail every time the bot is halted/resumed, and why."""
    __tablename__ = "kill_switch_log"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean)
    reason = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

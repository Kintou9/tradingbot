"""
FastAPI app entrypoint.

Run locally with:
    uvicorn api.main:app --reload

Endpoints here are for the dashboard (positions, trade history, P&L)
and the manual kill switch. Actual trading logic lives in /engine
and gets invoked by the Azure Functions in /functions, not by this
API directly (the API is for observing/controlling the bot, not
running the trading loop itself).
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import json
import os

from broker.alpaca_client import get_positions as get_broker_positions
from db.session import SessionLocal
from db.models import Trade, NewsItem, ResearchNote
from db.kill_switch import is_bot_enabled, set_bot_enabled
from engine.price_action_engine.market_data import fetch_ohlcv
from functions.after_hours_research import WATCHLIST

load_dotenv()

app = FastAPI(title="Trading Bot API")

# The dashboard is a separate Electron/Vite process on its own origin
# (localhost:5173 in dev, an opaque file:// origin once packaged) — needs
# CORS to call this API. Wide open is fine here: this API is bound to
# localhost for a single-user desktop app, not exposed publicly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "trading_mode": os.getenv("TRADING_MODE", "paper")}


@app.get("/status")
def status():
    return {"bot_enabled": is_bot_enabled()}


@app.post("/kill-switch")
def kill_switch():
    """Manual kill switch — halts the bot from taking new trades.
    Wire this up to a button on your dashboard."""
    set_bot_enabled(False, reason="manual kill switch")
    return {"bot_enabled": False, "message": "Trading halted."}


@app.post("/resume")
def resume():
    set_bot_enabled(True, reason="manual resume")
    return {"bot_enabled": True, "message": "Trading resumed."}


@app.get("/positions")
def get_positions():
    positions = get_broker_positions()
    return {
        "positions": [
            {
                "ticker": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
            }
            for p in positions
        ]
    }


@app.get("/trades")
def get_trades():
    db = SessionLocal()
    try:
        trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(100).all()
        return {
            "trades": [
                {
                    "ticker": t.ticker,
                    "action": t.action,
                    "quantity": t.quantity,
                    "price": t.price,
                    "timestamp": t.timestamp.isoformat(),
                    "reason": t.reason,
                }
                for t in trades
            ]
        }
    finally:
        db.close()


@app.get("/watchlist")
def get_watchlist():
    return {"watchlist": WATCHLIST}


@app.get("/news")
def get_news(ticker: str | None = None, limit: int = 50):
    db = SessionLocal()
    try:
        query = db.query(NewsItem)
        if ticker:
            query = query.filter(NewsItem.ticker == ticker.upper())
        items = query.order_by(NewsItem.ingested_at.desc()).limit(limit).all()
        return {
            "news": [
                {
                    "ticker": n.ticker,
                    "headline": n.headline,
                    "source": n.source,
                    "url": n.url,
                    "sentiment_score": n.sentiment_score,
                    "published_at": n.published_at.isoformat() if n.published_at else None,
                }
                for n in items
            ]
        }
    finally:
        db.close()


@app.get("/research")
def get_research(ticker: str | None = None, module: str | None = None, limit: int = 50):
    """Latest cached output from the after-hours LLM analyst modules
    (technical_scan, deep_dive, dcf) — see db/models.py ResearchNote."""
    db = SessionLocal()
    try:
        query = db.query(ResearchNote)
        if ticker:
            query = query.filter(ResearchNote.ticker == ticker.upper())
        if module:
            query = query.filter(ResearchNote.module == module)
        notes = query.order_by(ResearchNote.created_at.desc()).limit(limit).all()
        return {
            "research": [
                {
                    "ticker": n.ticker,
                    "module": n.module,
                    "verdict": n.verdict,
                    "confidence": n.confidence,
                    "raw_output": n.raw_output,
                    "structured_output": json.loads(n.structured_output) if n.structured_output else None,
                    "created_at": n.created_at.isoformat(),
                }
                for n in notes
            ]
        }
    finally:
        db.close()


@app.get("/chart/{ticker}")
def get_chart(ticker: str, interval: str = "1day", outputsize: int = 100):
    """Real OHLCV bars for the dashboard's price chart — same Twelve Data
    feed the technical scanner runs on, not a separate/fabricated source."""
    try:
        df = fetch_ohlcv(ticker.upper(), interval=interval, outputsize=outputsize)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch chart data: {exc}")

    bars = [
        {
            "date": index.strftime("%Y-%m-%d"),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for index, row in df.iterrows()
    ]
    return {"ticker": ticker.upper(), "bars": bars}

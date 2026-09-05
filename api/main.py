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

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import json
import os
import secrets

from broker.alpaca_client import get_account as get_broker_account, get_positions as get_broker_positions
from db.analytics import compute_trade_summary
from db.session import SessionLocal
from db.models import KillSwitchLog, Position, Trade, NewsItem, ResearchNote
from db.kill_switch import is_bot_enabled, set_bot_enabled
from engine.price_action_engine.market_data import fetch_ohlcv
from functions.after_hours_research import WATCHLIST

load_dotenv()

DASHBOARD_API_TOKEN = os.getenv("DASHBOARD_API_TOKEN")

# Mirrors functions/market_hours_trading.py's own constant — the dashboard
# only needs to *display* the limit, not enforce it, so it reads the same
# env var rather than importing that module (which stands up its own
# broker/trading-loop wiring at import time).
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))

app = FastAPI(title="Trading Bot API")

# The dashboard is a separate Electron/Vite process on its own origin
# (localhost:5173 in dev, an opaque "null" origin once packaged and
# loaded via file://). Token auth (below) is the real access control —
# CORS is browser-enforced only and does nothing against a non-browser
# client — but explicit origins are still worth it as defense in depth
# over a bare wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "null"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(x_api_token: str | None = Header(default=None)):
    """
    Every endpoint except /health requires this. Without it, CORS
    was the only thing standing between a malicious webpage open in the
    user's browser and this API's /kill-switch — CORS blocks a browser
    from reading the response, but with allow_origins=["*"] it doesn't
    stop the request from firing in the first place. A shared secret
    the attacker can't know closes that off regardless of origin.
    """
    if not DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=500, detail="DASHBOARD_API_TOKEN is not configured on the server.")
    if not x_api_token or not secrets.compare_digest(x_api_token, DASHBOARD_API_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")


@app.get("/health")
def health():
    return {"status": "ok", "trading_mode": os.getenv("TRADING_MODE", "paper")}


@app.get("/status", dependencies=[Depends(require_token)])
def status():
    db = SessionLocal()
    try:
        latest_log = db.query(KillSwitchLog).order_by(KillSwitchLog.id.desc()).first()
    finally:
        db.close()
    return {
        "bot_enabled": is_bot_enabled(),
        "kill_switch_reason": latest_log.reason if latest_log else None,
        "kill_switch_timestamp": latest_log.timestamp.isoformat() if latest_log else None,
    }


@app.get("/account", dependencies=[Depends(require_token)])
def get_account_summary():
    """Live equity + today's P&L from Alpaca, plus how much of the daily
    loss limit (Section 7 risk management) has been used up so far."""
    account = get_broker_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    daily_pl = equity - last_equity
    daily_pl_pct = (daily_pl / last_equity) if last_equity else 0.0
    loss_used_pct = max(0.0, -daily_pl_pct)
    return {
        "equity": equity,
        "daily_pl": daily_pl,
        "daily_pl_pct": daily_pl_pct,
        "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT,
        "daily_loss_headroom_pct": max(0.0, DAILY_LOSS_LIMIT_PCT - loss_used_pct),
    }


@app.post("/kill-switch", dependencies=[Depends(require_token)])
def kill_switch():
    """Manual kill switch — halts the bot from taking new trades.
    Wire this up to a button on your dashboard."""
    set_bot_enabled(False, reason="manual kill switch")
    return {"bot_enabled": False, "message": "Trading halted."}


@app.post("/resume", dependencies=[Depends(require_token)])
def resume():
    set_bot_enabled(True, reason="manual resume")
    return {"bot_enabled": True, "message": "Trading resumed."}


@app.get("/positions", dependencies=[Depends(require_token)])
def get_positions():
    positions = get_broker_positions()
    db = SessionLocal()
    try:
        # Live broker positions have no concept of "our" stop/take-profit
        # levels — those only exist in our own Position rows, keyed by
        # ticker, from when the bot opened the trade.
        tracked = {p.ticker: p for p in db.query(Position).all()}
    finally:
        db.close()
    return {
        "positions": [
            {
                "ticker": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "stop_loss_price": tracked[p.symbol].stop_loss_price if p.symbol in tracked else None,
                "take_profit_price": tracked[p.symbol].take_profit_price if p.symbol in tracked else None,
            }
            for p in positions
        ]
    }


@app.get("/trades", dependencies=[Depends(require_token)])
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


@app.get("/trades/summary", dependencies=[Depends(require_token)])
def get_trades_summary():
    """Realized P&L and win rate across all logged trades, via FIFO lot
    matching (see db/analytics.py) — /trades only returns raw fills."""
    db = SessionLocal()
    try:
        trades = db.query(Trade).order_by(Trade.timestamp.asc()).all()
        return compute_trade_summary(trades)
    finally:
        db.close()


@app.get("/watchlist", dependencies=[Depends(require_token)])
def get_watchlist():
    return {"watchlist": WATCHLIST}


@app.get("/news", dependencies=[Depends(require_token)])
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


@app.get("/research", dependencies=[Depends(require_token)])
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


@app.get("/chart/{ticker}", dependencies=[Depends(require_token)])
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

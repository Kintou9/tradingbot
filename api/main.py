"""
FastAPI app entrypoint.

Run locally with:
    uvicorn api.main:app --reload

The API owns the background protection/reconciliation and research workers,
alongside authenticated dashboard controls. Strategy and execution logic
live in /functions and /engine.
"""

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from datetime import date
from zoneinfo import ZoneInfo
import asyncio
import datetime as dt
import json
import os
import secrets
import uuid

from broker.alpaca_client import get_account as get_broker_account, get_positions as get_broker_positions, PAPER, assert_trading_mode
from db.analytics import compute_trade_summary
from db.session import SessionLocal, init_db
from db.models import (
    AutonomousModeLog, DiscoveryRunLog, GraphDecisionLog, KillSwitchLog, Position, Trade, NewsItem, ResearchNote,
    WatchedTicker, SwingCandidateLog, SwingPosition, SwingTrade,
)
from db.kill_switch import is_bot_enabled, set_bot_enabled
from db.watcher_retention import expire_watched, expires_at, run_watcher_cleanup
from db.research_status import research_state
from db.autonomous_mode import is_autonomous_enabled, set_autonomous_enabled
from db.swing_autonomous_mode import is_swing_autonomous_enabled, get_swing_autonomous_variant, set_swing_autonomous_enabled
from engine.entry_exit.execution import execute_buy, execute_sell
from engine.entry_exit.risk import Limits, RiskRejected
from db.execution_lock import ExecutionBusy
from notifications.monitoring import health_state, supervisor_tick
from fastapi.responses import JSONResponse
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.valuation_engine.fundamentals_data import get_company_name
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from functions.after_hours_research import WATCHLIST
from functions.market_hours_trading import MAX_POSITION_PCT, is_market_hours, run_market_hours_trading
from functions.swing_paper_trading import run_swing_paper_cycle
from functions.weekly_discovery import run_weekly_stock_discovery
from functions.scheduled_research import run_scheduled_research
from langgraph.types import Command
from agent.graph import get_graph
from agent.state import initial_state

load_dotenv()

DASHBOARD_API_TOKEN = os.getenv("DASHBOARD_API_TOKEN")

# Mirrors functions/market_hours_trading.py's own constant — the dashboard
# only needs to *display* the limit, not enforce it, so it reads the same
# env var rather than importing that module (which stands up its own
# broker/trading-loop wiring at import time).
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.02"))

# Protection/reconciliation runs independently of entry switches. The
# strategy's persisted entry cadence remains five minutes.
AUTONOMOUS_LOOP_INTERVAL_SECONDS = 30  # protection cadence; entries remain every 300s

app = FastAPI(title="Trading Bot API")


@app.on_event("startup")
def _create_tables_if_missing():
    init_db()


@app.on_event("startup")
async def _launch_autonomous_loop():
    async def loop():
        while True:
            try:
                # Always reconcile and protect existing holdings, even when
                # autonomous entries or the entry kill switch are disabled.
                await asyncio.to_thread(supervisor_tick, run_market_hours_trading)
            except Exception as exc:
                print(f"[autonomous] trading cycle failed: {exc}")
            await asyncio.sleep(AUTONOMOUS_LOOP_INTERVAL_SECONDS)

    asyncio.create_task(loop())


@app.on_event("startup")
async def _launch_research_loop():
    async def loop():
        while True:
            try:
                await asyncio.to_thread(run_scheduled_research)
            except Exception as exc:
                print(f"[research] scheduled run failed: {type(exc).__name__}")
            await asyncio.sleep(300)
    asyncio.create_task(loop())


@app.on_event("startup")
async def _launch_watcher_cleanup():
    async def loop():
        while True:
            try:
                await asyncio.to_thread(run_watcher_cleanup)
            except Exception as exc:
                print(f"[watcher] cleanup failed: {type(exc).__name__}")
            await asyncio.sleep(60)
    asyncio.create_task(loop())


# How often the weekly-discovery loop wakes up to check whether it's due
# (Friday, after market close, not already run this week). Coarser than
# the trading loop's interval on purpose — this only needs to notice
# "it's now past 4pm ET on a Friday" sometime before end of day, not
# within seconds of it.
DISCOVERY_CHECK_INTERVAL_SECONDS = 1800
DISCOVERY_DAY_OF_WEEK = 4  # Monday=0 ... Friday=4
DISCOVERY_HOUR_ET = 16  # 4pm


def _discovery_already_ran_today(db) -> bool:
    today = date.today()
    return db.query(DiscoveryRunLog).filter(DiscoveryRunLog.run_date == today).first() is not None


def _discovery_is_due() -> bool:
    now_et = dt.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() != DISCOVERY_DAY_OF_WEEK or now_et.hour < DISCOVERY_HOUR_ET:
        return False
    db = SessionLocal()
    try:
        return not _discovery_already_ran_today(db)
    finally:
        db.close()


@app.on_event("startup")
async def _launch_discovery_loop():
    async def loop():
        while True:
            try:
                if _discovery_is_due():
                    added = await asyncio.to_thread(run_weekly_stock_discovery)
                    print(f"[discovery] weekly run added: {[a['ticker'] for a in added]}")
            except Exception as exc:
                print(f"[discovery] weekly run failed: {exc}")
            await asyncio.sleep(DISCOVERY_CHECK_INTERVAL_SECONDS)

    asyncio.create_task(loop())


# How often the swing-strategy autonomous loop checks in — deliberately
# tighter than the live loop's 5-minute cadence isn't needed here either;
# same interval, same reasoning (run_swing_paper_cycle is cheap for
# variant A/C — no LLM calls — and this only controls how promptly a
# newly-confirmed setup gets acted on while the toggle is on).
SWING_AUTONOMOUS_INTERVAL_SECONDS = 300


@app.on_event("startup")
async def _launch_swing_autonomous_loop():
    async def loop():
        while True:
            try:
                if is_swing_autonomous_enabled() and is_market_hours():
                    variant = get_swing_autonomous_variant(default="A")
                    config = SwingStrategyConfig(gate_variant=GateVariant(variant))
                    # execute=True: places real Alpaca PAPER orders into the
                    # isolated swing_positions/swing_trades tables — never
                    # the live Position/Trade tables, never real money (see
                    # engine/swing_strategy/README.md and
                    # tests/test_swing_preserves_live_behavior.py for the
                    # isolation guarantees this relies on).
                    summary = await asyncio.to_thread(run_swing_paper_cycle, config, True)
                    if summary["accepted"] or summary["exits"]:
                        print(f"[swing-autonomous] variant {variant}: accepted={summary['accepted']} exits={summary['exits']}")
            except Exception as exc:
                print(f"[swing-autonomous] cycle failed: {exc}")
            await asyncio.sleep(SWING_AUTONOMOUS_INTERVAL_SECONDS)

    asyncio.create_task(loop())

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
    return {"status": "ok", "trading_mode": "paper" if PAPER else "live"}


@app.get("/health/ready")
def readiness():
    db = SessionLocal()
    try:
        state = health_state(db)
        # Public monitor gets only aggregate health, no account details.
        return JSONResponse(status_code=200 if state["healthy"] else 503,
                            content={"healthy": state["healthy"]})
    except Exception:
        return JSONResponse(status_code=503, content={"healthy": False})
    finally:
        db.close()


@app.get("/status", dependencies=[Depends(require_token)])
def status():
    db = SessionLocal()
    try:
        latest_log = db.query(KillSwitchLog).order_by(KillSwitchLog.id.desc()).first()
        monitoring = health_state(db)
    finally:
        db.close()
    return {
        "bot_enabled": is_bot_enabled(),
        "trading_mode": "paper" if PAPER else "live",
        "monitoring": monitoring,
        "limits": vars(Limits.from_env()),
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
    return {"bot_enabled": False, "message": "New entries paused. Existing-position protection continues."}


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


class TradeRequest(BaseModel):
    qty: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    stop_loss_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    take_profit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


@app.post("/positions/{ticker}/buy", dependencies=[Depends(require_token)])
def buy_position(ticker: str, body: TradeRequest = TradeRequest()):
    """Manual entries use exactly the same broker protection and risk limits."""
    ticker = ticker.upper()
    db = SessionLocal()
    try:
        from functions.market_hours_trading import _latest_research
        research = _latest_research(db, ticker, "technical_scan") or {}
        return execute_buy(db, ticker, body.qty, reason="manual",
            stop_loss_price=body.stop_loss_price or research.get("stop_loss"),
            take_profit_price=body.take_profit_price or research.get("target_1"))
    except (RiskRejected, ExecutionBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()


@app.post("/positions/{ticker}/sell", dependencies=[Depends(require_token)])
def sell_position(ticker: str, body: TradeRequest = TradeRequest()):
    """Manual sell — closes the full position unless qty is overridden."""
    ticker = ticker.upper()
    held = {p.symbol: p for p in get_broker_positions()}.get(ticker)
    if held is None:
        raise HTTPException(status_code=404, detail=f"No open {ticker} position.")
    qty = body.qty if body.qty is not None else float(held.qty)

    db = SessionLocal()
    try:
        try:
            return execute_sell(db, ticker, qty, reason="manual")
        except (RiskRejected, ExecutionBusy) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()


class GraphResumeRequest(BaseModel):
    decision: str
    feedback: str | None = None


def _graph_response(result: dict, thread_id: str) -> dict:
    interrupts = result.get("__interrupt__")
    if interrupts:
        return {"thread_id": thread_id, "status": "pending_approval", **interrupts[0].value}
    return {
        "thread_id": thread_id,
        "status": result.get("terminal_reason"),
        "ticker": result.get("ticker"),
        "proposed_signal": result.get("proposed_signal"),
        "execution_result": result.get("execution_result"),
        "execution_error": result.get("execution_error"),
        "engine_errors": result.get("engine_errors"),
    }


def _decision_log_json(row, *field_names):
    return {name: (json.loads(getattr(row, f"{name}_json")) if getattr(row, f"{name}_json") else None)
        for name in field_names}


@app.post("/graph/{ticker}/run", dependencies=[Depends(require_token)])
def run_graph(ticker: str):
    """Start a supervised (human-approval-gated) trade evaluation. Reuses
    the same deterministic engines/execution as the autonomous loop, but
    pauses before any paper trade for explicit approval via /graph/{thread_id}/resume."""
    ticker = ticker.upper()
    thread_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    result = get_graph().invoke(initial_state(ticker, thread_id), config=config)
    return _graph_response(result, thread_id)


@app.post("/graph/{thread_id}/resume", dependencies=[Depends(require_token)])
def resume_graph(thread_id: str, body: GraphResumeRequest):
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    if not graph.get_state(config).next:
        raise HTTPException(status_code=409, detail="No pending approval for this thread_id")
    result = graph.invoke(Command(resume={"decision": body.decision, "feedback": body.feedback}), config=config)
    return _graph_response(result, thread_id)


@app.get("/graph/pending", dependencies=[Depends(require_token)])
def list_pending_graph_decisions():
    db = SessionLocal()
    try:
        rows = (db.query(GraphDecisionLog).filter_by(status="pending_approval")
            .order_by(GraphDecisionLog.created_at.desc()).all())
        return {"pending": [{
            "thread_id": r.thread_id, "ticker": r.ticker, "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            **_decision_log_json(r, "proposed_signal"),
        } for r in rows]}
    finally:
        db.close()


@app.get("/graph/{thread_id}", dependencies=[Depends(require_token)])
def get_graph_decision(thread_id: str):
    db = SessionLocal()
    try:
        row = db.query(GraphDecisionLog).filter_by(thread_id=thread_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown thread_id")
        return {
            "thread_id": row.thread_id, "ticker": row.ticker, "status": row.status,
            "sentiment_score": row.sentiment_score, "retry_count": row.retry_count,
            "human_decision": row.human_decision, "human_feedback": row.human_feedback,
            "execution_error": row.execution_error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            **_decision_log_json(row, "dcf_result", "technical_result", "proposed_signal",
                "llm_analysis", "validation_result", "execution_result"),
        }
    finally:
        db.close()


@app.get("/autonomous", dependencies=[Depends(require_token)])
def autonomous_status():
    db = SessionLocal()
    try:
        latest = db.query(AutonomousModeLog).order_by(AutonomousModeLog.id.desc()).first()
    finally:
        db.close()
    return {
        "autonomous_enabled": is_autonomous_enabled(),
        "reason": latest.reason if latest else None,
        "timestamp": latest.timestamp.isoformat() if latest else None,
    }


@app.post("/autonomous/enable", dependencies=[Depends(require_token)])
def enable_autonomous():
    """Enable unattended entries, subject to shared risk and monitoring gates.
    Existing-position protection runs independently of this switch."""
    try:
        assert_trading_mode()
        Limits.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    set_autonomous_enabled(True, reason="manual enable")
    return {"autonomous_enabled": True}


@app.post("/autonomous/disable", dependencies=[Depends(require_token)])
def disable_autonomous():
    set_autonomous_enabled(False, reason="manual disable")
    return {"autonomous_enabled": False}


class SwingAutonomousEnableRequest(BaseModel):
    variant: str = "A"  # "A" | "B" | "C" | "D" — see engine.swing_strategy.config.GateVariant


@app.get("/swing/autonomous", dependencies=[Depends(require_token)])
def swing_autonomous_status():
    return {
        "swing_autonomous_enabled": is_swing_autonomous_enabled(),
        "variant": get_swing_autonomous_variant(),
    }


@app.post("/swing/autonomous/enable", dependencies=[Depends(require_token)])
def enable_swing_autonomous(body: SwingAutonomousEnableRequest = SwingAutonomousEnableRequest()):
    """Turns on unattended PAPER trading for the experimental swing
    strategy — separate from /autonomous/enable (the live strategy's
    switch). Places real Alpaca paper orders into swing_positions/
    swing_trades only; never touches the live Position/Trade tables or
    real money."""
    if body.variant not in ("A", "B", "C", "D"):
        raise HTTPException(status_code=400, detail="variant must be one of A, B, C, D")
    if not PAPER:
        raise HTTPException(status_code=409, detail="Swing execution is paper-only")
    set_swing_autonomous_enabled(True, variant=body.variant, reason="manual enable")
    return {"swing_autonomous_enabled": True, "variant": body.variant}


@app.post("/swing/autonomous/disable", dependencies=[Depends(require_token)])
def disable_swing_autonomous():
    set_swing_autonomous_enabled(False, variant=get_swing_autonomous_variant(), reason="manual disable")
    return {"swing_autonomous_enabled": False}


@app.get("/swing/positions", dependencies=[Depends(require_token)])
def get_swing_positions():
    db = SessionLocal()
    try:
        positions = db.query(SwingPosition).filter(SwingPosition.status == "open").order_by(SwingPosition.entry_timestamp.desc()).all()
        return {
            "positions": [
                {
                    "ticker": p.ticker, "quantity": p.quantity, "entry_price": p.entry_price,
                    "stop_price": p.stop_price, "target_price": p.target_price,
                    "entry_timestamp": p.entry_timestamp.isoformat(), "strategy_version": p.strategy_version,
                }
                for p in positions
            ]
        }
    finally:
        db.close()


@app.get("/swing/candidates", dependencies=[Depends(require_token)])
def get_swing_candidates(limit: int = 50):
    db = SessionLocal()
    try:
        candidates = db.query(SwingCandidateLog).order_by(SwingCandidateLog.id.desc()).limit(limit).all()
        return {
            "candidates": [
                {
                    "ticker": c.ticker, "variant": c.variant, "accepted": c.accepted,
                    "rejection_reason": c.rejection_reason, "entry_price": c.entry_price,
                    "stop_price": c.stop_price, "target_price": c.target_price,
                    "reward_to_risk": c.reward_to_risk, "shares": c.shares,
                    "planned_dollar_risk": c.planned_dollar_risk,
                    "signal_timestamp": c.signal_timestamp.isoformat() if c.signal_timestamp else None,
                }
                for c in candidates
            ]
        }
    finally:
        db.close()


@app.get("/swing/trades", dependencies=[Depends(require_token)])
def get_swing_trades(limit: int = 100):
    db = SessionLocal()
    try:
        trades = db.query(SwingTrade).order_by(SwingTrade.timestamp.desc()).limit(limit).all()
        return {
            "trades": [
                {"ticker": t.ticker, "action": t.action, "quantity": t.quantity, "price": t.price,
                 "reason": t.reason, "timestamp": t.timestamp.isoformat()}
                for t in trades
            ]
        }
    finally:
        db.close()


@app.get("/discovery", dependencies=[Depends(require_token)])
def discovery_status():
    """Latest weekly-discovery run, plus whether one is scheduled to fire
    later today (Friday after 4pm ET) — see the startup loop above."""
    db = SessionLocal()
    try:
        latest = db.query(DiscoveryRunLog).order_by(DiscoveryRunLog.id.desc()).first()
    finally:
        db.close()
    now_et = dt.datetime.now(ZoneInfo("America/New_York"))
    return {
        "last_run_date": latest.run_date.isoformat() if latest else None,
        "last_run_tickers": latest.tickers.split(", ") if latest and latest.tickers else [],
        "due_today": _discovery_is_due(),
        "current_time_et": now_et.isoformat(),
    }


@app.post("/discovery/run-now", dependencies=[Depends(require_token)])
def discovery_run_now():
    """Manual trigger — runs discovery immediately regardless of day/time,
    for testing or if you don't want to wait for Friday. Still records a
    DiscoveryRunLog row, so it counts as this week's run."""
    added = run_weekly_stock_discovery()
    return {"added": added}


@app.get("/recommendations", dependencies=[Depends(require_token)])
def get_recommendations():
    """Candidates not currently held — the bot's own WATCHLIST plus the
    user's personal Watcher list — scored against the same gates
    evaluate_entry uses, from whatever research is already cached. Never
    triggers new research itself; the separate worker researches Watcher
    candidates without authorizing trading."""
    db = SessionLocal()
    try:
        expire_watched(db)
        held_tickers = {p.symbol for p in get_broker_positions()}
        watched_tickers = [w.ticker for w in db.query(WatchedTicker).all()]
        candidates = sorted((set(watched_tickers) | set(WATCHLIST)) - held_tickers)

        def latest_note(ticker, module):
            return (
                db.query(ResearchNote)
                .filter(ResearchNote.ticker == ticker, ResearchNote.module == module)
                .order_by(ResearchNote.created_at.desc())
                .first()
            )

        results = []
        for ticker in candidates:
            technical_note = latest_note(ticker, "technical_scan")
            dcf_note = latest_note(ticker, "dcf")
            technical = json.loads(technical_note.structured_output) if technical_note and technical_note.structured_output else None
            dcf = json.loads(dcf_note.structured_output) if dcf_note and dcf_note.structured_output else None
            sentiment_items = db.query(NewsItem).filter(NewsItem.ticker == ticker).order_by(NewsItem.ingested_at.desc()).limit(20).all()
            scores = [n.sentiment_score for n in sentiment_items if n.sentiment_score is not None]
            avg_sentiment = sum(scores) / len(scores) if scores else 0.0

            clears = False
            discount_pct = None
            if dcf is None:
                reason = "No DCF yet — valuation unknown"
            elif dcf.get("share_count_source") != "filing":
                # Fail closed: covers an explicit "unavailable" AND every
                # DCF cached before this field existed (see the matching
                # guard in engine.entry_exit.signals.evaluate_entry).
                reason = "DCF unreliable — filed share count unavailable"
            elif technical is None:
                reason = "No technical scan yet"
            elif technical.get("trend") == "down":
                reason = "Downtrend"
            elif avg_sentiment < 0:
                reason = f"Negative recent sentiment ({avg_sentiment:.2f})"
            else:
                fair_value = dcf.get("estimated_fair_value_per_share")
                entry_price = technical.get("entry_price")
                if not fair_value or not entry_price:
                    reason = "Incomplete research data"
                else:
                    discount_pct = (fair_value - entry_price) / entry_price * 100
                    if discount_pct < 20:
                        reason = f"Only {discount_pct:.0f}% below fair value (needs 20%+)"
                    else:
                        clears = True
                        reason = f"{discount_pct:.0f}% below fair value, uptrend"

            try:
                company_name = get_company_name(ticker)
            except Exception:
                company_name = None

            results.append({
                "ticker": ticker,
                "company_name": company_name,
                "clears_entry_gate": clears,
                "discount_pct": discount_pct,
                "reason": reason,
                "on_watchlist": ticker in WATCHLIST,
                "on_watcher": ticker in watched_tickers,
                "researched_at": technical_note.created_at.isoformat() if technical_note else None,
            })

        results.sort(key=lambda r: (not r["clears_entry_gate"], -(r["discount_pct"] if r["discount_pct"] is not None else -999)))
        return {"recommendations": results}
    finally:
        db.close()


class WatchedTickerCreate(BaseModel):
    ticker: str
    notes: str | None = None


@app.get("/watched", dependencies=[Depends(require_token)])
def get_watched():
    """Watcher candidates, research freshness and latest assessments.
    The research queue includes these candidates independently of trading."""
    db = SessionLocal()
    try:
        expire_watched(db)
        watched = db.query(WatchedTicker).order_by(WatchedTicker.added_at.desc()).all()
        result = []
        for w in watched:
            deep_dive = (
                db.query(ResearchNote)
                .filter(ResearchNote.ticker == w.ticker, ResearchNote.module == "deep_dive")
                .order_by(ResearchNote.created_at.desc())
                .first()
            )
            technical = (
                db.query(ResearchNote)
                .filter(ResearchNote.ticker == w.ticker, ResearchNote.module == "technical_scan")
                .order_by(ResearchNote.created_at.desc())
                .first()
            )
            try:
                company_name = get_company_name(w.ticker)
            except Exception:
                company_name = None
            result.append({
                "ticker": w.ticker,
                "company_name": company_name,
                "notes": w.notes,
                "added_at": w.added_at.isoformat() + "Z",
                "expires_at": expires_at(w).isoformat() + "Z" if expires_at(w) else None,
                "research": research_state(db, w.ticker),
                "latest_verdict": deep_dive.verdict if deep_dive else None,
                "latest_trend": technical.verdict if technical else None,
                "deep_dive": {
                    "structured": json.loads(deep_dive.structured_output) if deep_dive and deep_dive.structured_output else None,
                    "report": deep_dive.raw_output,
                    "created_at": deep_dive.created_at.isoformat() + "Z",
                } if deep_dive else None,
                "technical": {
                    "structured": json.loads(technical.structured_output) if technical and technical.structured_output else None,
                    "created_at": technical.created_at.isoformat() + "Z",
                } if technical else None,
            })
        return {"watched": result}
    finally:
        db.close()


@app.post("/watched", dependencies=[Depends(require_token)])
def add_watched(body: WatchedTickerCreate):
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is required.")
    db = SessionLocal()
    try:
        expire_watched(db)
        existing = db.query(WatchedTicker).filter(WatchedTicker.ticker == ticker).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"{ticker} is already on the watch list.")
        db.add(WatchedTicker(ticker=ticker, notes=body.notes))
        db.commit()
        return {"ticker": ticker, "notes": body.notes}
    finally:
        db.close()


@app.delete("/watched/{ticker}", dependencies=[Depends(require_token)])
def remove_watched(ticker: str):
    db = SessionLocal()
    try:
        existing = db.query(WatchedTicker).filter(WatchedTicker.ticker == ticker.upper()).first()
        if not existing:
            raise HTTPException(status_code=404, detail=f"{ticker.upper()} is not on the watch list.")
        db.delete(existing)
        db.commit()
        return {"removed": ticker.upper()}
    finally:
        db.close()


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

"""Graph nodes. Each is a thin wrapper around an existing engine/execution
function — no engine math is reimplemented here (see the plan's "Pitfalls
to avoid"). Every node opens and closes its own short-lived DB session;
none of them hold `db` across the human-approval pause.
"""
import json
import os
from datetime import datetime

from pydantic import ValidationError

from broker.alpaca_client import get_account
from db.execution_lock import ExecutionBusy
from db.models import GraphDecisionLog
from db.session import SessionLocal
from engine.entry_exit.execution import execute_buy
from engine.entry_exit.risk import Limits, RiskRejected
from engine.entry_exit.signals import evaluate_entry
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.technical_scanner import run_technical_scan
from engine.valuation_engine.dcf import run_dcf
from engine.valuation_engine.fundamentals_data import get_company_name, get_financial_summary
from functions.after_hours_research import _save_note
from functions.market_hours_trading import _avg_sentiment, _latest_research
from langgraph.types import interrupt

from agent.llm_synthesis import run_llm_synthesis
from agent.schemas import LLMSynthesisOutput

MAX_SYNTHESIS_RETRIES = int(os.getenv("LLM_SYNTHESIS_MAX_RETRIES", "2"))


# --- Data acquisition (cache-first, matching the autonomous loop's own
# 24h freshness check; a fresh fetch is persisted back to the same cache) ---

def run_valuation(state):
    ticker = state["ticker"]
    db = SessionLocal()
    try:
        cached = _latest_research(db, ticker, "dcf")
        if cached is not None:
            return {"dcf_result": cached}
        company_name = get_company_name(ticker)
        filing_context = get_financial_summary(ticker)
        result = run_dcf(ticker, company_name, filing_context)
        _save_note(db, ticker, "dcf", result)
        return {"dcf_result": result["structured"]}
    except Exception as exc:
        return {"engine_errors": [f"dcf: {type(exc).__name__}"]}
    finally:
        db.close()


def run_technical(state):
    ticker = state["ticker"]
    db = SessionLocal()
    try:
        cached = _latest_research(db, ticker, "technical_scan")
        if cached is not None:
            return {"technical_result": cached}
        ohlcv = fetch_ohlcv(ticker)
        result = run_technical_scan(ticker, ohlcv)
        _save_note(db, ticker, "technical_scan", result)
        return {"technical_result": result["structured"]}
    except Exception as exc:
        return {"engine_errors": [f"technical_scan: {type(exc).__name__}"]}
    finally:
        db.close()


def aggregate_sentiment(state):
    db = SessionLocal()
    try:
        return {"sentiment_score": _avg_sentiment(db, state["ticker"])}
    except Exception:
        return {"sentiment_score": 0.0}  # same fail-open neutral default _avg_sentiment itself uses
    finally:
        db.close()


# --- Deterministic gate ---

def evaluate_signal(state):
    if state["dcf_result"] is None or state["technical_result"] is None:
        return {"terminal_reason": "engine_error"}
    try:
        account = get_account()
        limits = Limits.from_env()
        risk_snapshot = {
            "account_equity": float(account.equity),
            "max_position_pct": limits.max_position_pct,
            "max_trade_notional": limits.max_trade,
            "max_trades_per_day": limits.max_entries,
            "daily_loss_pct": limits.daily_loss_pct,
        }
        signal = evaluate_entry(
            state["ticker"],
            state["dcf_result"],
            state["technical_result"],
            state["sentiment_score"] or 0.0,
            max_position_pct=limits.max_position_pct,
            account_equity=float(account.equity),
        )
    except Exception as exc:
        return {"engine_errors": [f"evaluate_signal: {type(exc).__name__}"], "terminal_reason": "engine_error"}
    if signal is None:
        return {"risk_snapshot": risk_snapshot, "terminal_reason": "no_signal"}
    proposed_signal = {
        "ticker": signal.ticker,
        "action": signal.action.value,
        "reason": signal.reason,
        "confidence": signal.confidence,
    }
    return {"risk_snapshot": risk_snapshot, "proposed_signal": proposed_signal}


def route_after_evaluate(state):
    return "llm_synthesis" if state.get("proposed_signal") else "finalize"


# --- LLM synthesis + guardrail (the only retry loop in this graph) ---

def llm_synthesis(state):
    validation = state.get("validation_result")
    prior_errors = validation["errors"] if validation and not validation["valid"] else None
    try:
        result = run_llm_synthesis(
            state["ticker"], state["dcf_result"], state["technical_result"],
            state["sentiment_score"] or 0.0, state["proposed_signal"],
            prior_errors=prior_errors,
        )
        analysis = dict(result["structured"])
        analysis["raw_output"] = result["raw_output"]
        return {"llm_analysis": analysis}
    except Exception:
        return {"llm_analysis": None}


def _numeric_mismatch(cited, actual):
    if cited is None or actual is None:
        return False
    return abs(cited - actual) > max(0.01, abs(actual) * 0.01)


def validate_synthesis(state):
    analysis = state.get("llm_analysis")
    errors = []
    if analysis is None:
        errors.append("llm_synthesis produced no output")
    else:
        try:
            validated = LLMSynthesisOutput.model_validate(analysis)
        except ValidationError as exc:
            errors.append(f"schema: {exc.error_count()} field error(s)")
        else:
            dcf_fv = (state["dcf_result"] or {}).get("estimated_fair_value_per_share")
            if _numeric_mismatch(validated.cited_fair_value, dcf_fv):
                errors.append("cited_fair_value does not match the supplied DCF result")
            tech_entry = (state["technical_result"] or {}).get("entry_price")
            if _numeric_mismatch(validated.cited_entry_price, tech_entry):
                errors.append("cited_entry_price does not match the supplied technical result")

    technical = state["technical_result"] or {}
    if technical.get("stop_loss") is None or technical.get("target_1") is None:
        errors.append("proposed trade is missing stop_loss/target_1 from technical analysis")

    risk = state.get("risk_snapshot") or {}
    if risk and risk.get("account_equity", 0) * risk.get("max_position_pct", 0) < 1.0:
        errors.append("position size budget below minimum tradable notional")

    valid = not errors
    update = {"validation_result": {"valid": valid, "errors": errors}}
    if not valid:
        retry_count = state["validation_retry_count"] + 1
        update["validation_retry_count"] = retry_count
        if retry_count > MAX_SYNTHESIS_RETRIES:
            update["terminal_reason"] = "validation_exhausted"
    return update


def route_after_validation(state):
    if state["validation_result"]["valid"]:
        return "record_pending_approval"
    if state.get("terminal_reason") == "validation_exhausted":
        return "finalize"
    return "llm_synthesis"


# --- Human approval ---

def record_pending_approval(state):
    db = SessionLocal()
    try:
        row = db.query(GraphDecisionLog).filter_by(thread_id=state["thread_id"]).first()
        if row is None:
            row = GraphDecisionLog(thread_id=state["thread_id"], ticker=state["ticker"])
            db.add(row)
        row.dcf_result_json = json.dumps(state["dcf_result"])
        row.technical_result_json = json.dumps(state["technical_result"])
        row.sentiment_score = state["sentiment_score"]
        row.proposed_signal_json = json.dumps(state["proposed_signal"])
        row.llm_analysis_json = json.dumps(state["llm_analysis"])
        row.validation_result_json = json.dumps(state["validation_result"])
        row.retry_count = state["validation_retry_count"]
        row.status = "pending_approval"
        db.commit()
        db.refresh(row)
        return {"decision_log_id": row.id}
    finally:
        db.close()


def await_approval(state):
    payload = {
        "ticker": state["ticker"],
        "proposed_signal": state["proposed_signal"],
        "dcf_summary": state["dcf_result"],
        "technical_summary": state["technical_result"],
        "sentiment_score": state["sentiment_score"],
        "risk_snapshot": state["risk_snapshot"],
        "llm_analysis": state["llm_analysis"],
        "validation_result": state["validation_result"],
        "decision_log_id": state["decision_log_id"],
    }
    response = interrupt(payload)
    decision = response.get("decision")
    update = {"human_decision": decision, "human_feedback": response.get("feedback")}
    if decision != "approve":
        update["terminal_reason"] = "rejected"
    return update


def route_after_approval(state):
    return "execute_trade" if state["human_decision"] == "approve" else "finalize"


# --- Execution (sole authority: execute_buy re-validates everything itself) ---

def execute_trade(state):
    db = SessionLocal()
    try:
        technical = state["technical_result"] or {}
        signal = state["proposed_signal"]
        result = execute_buy(
            db, state["ticker"], reason=signal["reason"],
            stop_loss_price=technical.get("stop_loss"),
            take_profit_price=technical.get("target_1"),
            strategy="primary",
        )
        return {"execution_result": result, "terminal_reason": "executed"}
    except (RiskRejected, RuntimeError, ExecutionBusy) as exc:
        db.rollback()
        return {"execution_error": f"{type(exc).__name__}: {exc}", "terminal_reason": "execution_failed"}
    finally:
        db.close()


# --- Terminal writer (single writer for every path, success or not) ---

def finalize(state):
    db = SessionLocal()
    try:
        row = None
        if state.get("decision_log_id"):
            row = db.get(GraphDecisionLog, state["decision_log_id"])
        if row is None:
            row = db.query(GraphDecisionLog).filter_by(thread_id=state["thread_id"]).first()
        if row is None:
            row = GraphDecisionLog(thread_id=state["thread_id"], ticker=state["ticker"])
            db.add(row)
        if row.dcf_result_json is None and state.get("dcf_result") is not None:
            row.dcf_result_json = json.dumps(state["dcf_result"])
        if row.technical_result_json is None and state.get("technical_result") is not None:
            row.technical_result_json = json.dumps(state["technical_result"])
        if row.proposed_signal_json is None and state.get("proposed_signal") is not None:
            row.proposed_signal_json = json.dumps(state["proposed_signal"])
        if row.sentiment_score is None and state.get("sentiment_score") is not None:
            row.sentiment_score = state["sentiment_score"]
        if row.llm_analysis_json is None and state.get("llm_analysis") is not None:
            row.llm_analysis_json = json.dumps(state["llm_analysis"])
        if row.validation_result_json is None and state.get("validation_result") is not None:
            row.validation_result_json = json.dumps(state["validation_result"])
        if not row.retry_count:
            row.retry_count = state["validation_retry_count"]
        row.status = state["terminal_reason"] or "unknown"
        row.human_decision = state.get("human_decision")
        row.human_feedback = state.get("human_feedback")
        row.execution_result_json = json.dumps(state.get("execution_result"))
        row.execution_error = state.get("execution_error")
        row.resolved_at = datetime.utcnow()
        db.commit()
        return {}
    finally:
        db.close()

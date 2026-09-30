"""Graph-level tests for agent/ (LangGraph supervised trade-decision
workflow). Tests state threading, routing, retry, and interrupt/resume —
NOT execute_buy/execute_sell internals, which tests/test_execution_safety.py
already covers exhaustively. All engine/LLM/broker calls are mocked at the
function boundary in agent.nodes, same convention as tests/test_watcher_research.py.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, GraphDecisionLog
from db.execution_lock import ExecutionBusy
from engine.entry_exit.risk import RiskRejected
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent import nodes
from agent.graph import build_graph
from agent.state import initial_state

DCF_OK = {"estimated_fair_value_per_share": 150.0, "share_count_source": "filing"}
TECH_OK = {"trend": "up", "entry_price": 100.0, "stop_loss": 90.0, "target_1": 130.0, "probability_estimate": 70}
LLM_ANALYSIS_OK = {
    "agreement": True, "confidence": 80, "reasoning": "Discount looks real and uptrend is confirmed.",
    "notable_risks": ["macro risk"],
    "cited_fair_value": DCF_OK["estimated_fair_value_per_share"],
    "cited_entry_price": TECH_OK["entry_price"],
}


class FakeAccount:
    equity = "10000"
    last_equity = "10000"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)

    calls = {"run_dcf": 0, "run_technical_scan": 0, "run_llm_synthesis": 0, "execute_buy": 0}

    monkeypatch.setattr(nodes, "SessionLocal", factory)
    monkeypatch.setattr(nodes, "_latest_research", lambda db, ticker, module: None)
    monkeypatch.setattr(nodes, "_save_note", lambda db, ticker, module, result: None)
    monkeypatch.setattr(nodes, "get_company_name", lambda ticker: ticker)
    monkeypatch.setattr(nodes, "get_financial_summary", lambda ticker: "financials")
    monkeypatch.setattr(nodes, "fetch_ohlcv", lambda ticker: object())

    def fake_run_dcf(*a, **kw):
        calls["run_dcf"] += 1
        return {"raw_output": "dcf", "structured": dict(DCF_OK)}

    def fake_run_technical_scan(*a, **kw):
        calls["run_technical_scan"] += 1
        return {"raw_output": "tech", "structured": dict(TECH_OK)}

    def fake_execute_buy(db, ticker, **kw):
        calls["execute_buy"] += 1
        return {"ticker": ticker, "action": "buy", "qty": 5, "price": 100.0,
            "order_id": "abc123", "status": "filled", "requested_qty": 5}

    monkeypatch.setattr(nodes, "run_dcf", fake_run_dcf)
    monkeypatch.setattr(nodes, "run_technical_scan", fake_run_technical_scan)
    monkeypatch.setattr(nodes, "_avg_sentiment", lambda db, ticker: 0.5)
    monkeypatch.setattr(nodes, "get_account", lambda: FakeAccount())
    monkeypatch.setattr(nodes, "run_llm_synthesis", lambda *a, **kw: {
        "raw_output": "analysis", "structured": dict(LLM_ANALYSIS_OK)})
    monkeypatch.setattr(nodes, "execute_buy", fake_execute_buy)

    graph = build_graph().compile(checkpointer=InMemorySaver())
    return {"graph": graph, "factory": factory, "calls": calls}


def _run(env, ticker="MSFT", thread_id="t-1"):
    config = {"configurable": {"thread_id": thread_id}}
    result = env["graph"].invoke(initial_state(ticker, thread_id), config=config)
    return result, config


def _log_row(env, thread_id="t-1"):
    db = env["factory"]()
    try:
        return db.query(GraphDecisionLog).filter_by(thread_id=thread_id).first()
    finally:
        db.close()


# --- Successful path: run -> interrupt -> approve -> executed ---

def test_successful_execution_end_to_end(env):
    result, config = _run(env)
    assert result["__interrupt__"], "expected the graph to pause for approval"
    payload = result["__interrupt__"][0].value
    assert payload["ticker"] == "MSFT"
    assert payload["proposed_signal"]["action"] == "buy"
    assert payload["llm_analysis"]["agreement"] is True

    final = env["graph"].invoke(Command(resume={"decision": "approve"}), config=config)
    assert final["terminal_reason"] == "executed"
    assert final["execution_result"]["status"] == "filled"
    assert env["calls"]["execute_buy"] == 1

    row = _log_row(env)
    assert row.status == "executed"
    assert row.human_decision == "approve"


# --- Guardrail retry: invalid cited number -> retry -> valid -> approval ---

def test_validation_failure_then_retry_then_success(env, monkeypatch):
    attempts = {"n": 0}

    def flaky_synthesis(*a, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            bad = dict(LLM_ANALYSIS_OK)
            bad["cited_fair_value"] = 9999.0  # doesn't match DCF_OK -> guardrail rejects
            return {"raw_output": "bad", "structured": bad}
        return {"raw_output": "good", "structured": dict(LLM_ANALYSIS_OK)}

    monkeypatch.setattr(nodes, "run_llm_synthesis", flaky_synthesis)

    result, _ = _run(env)
    assert attempts["n"] == 2
    assert result["__interrupt__"], "should still reach approval after a successful retry"
    assert result["validation_retry_count"] == 1
    assert result["validation_result"]["valid"] is True


# --- Retry exhaustion: guardrail always invalid -> terminates safely, no trade ---

def test_retry_exhaustion_terminates_safely(env, monkeypatch):
    monkeypatch.setattr(nodes, "MAX_SYNTHESIS_RETRIES", 1)

    def always_bad(*a, **kw):
        bad = dict(LLM_ANALYSIS_OK)
        bad["cited_fair_value"] = -1.0
        return {"raw_output": "bad", "structured": bad}

    monkeypatch.setattr(nodes, "run_llm_synthesis", always_bad)

    result, _ = _run(env)
    assert "__interrupt__" not in result
    assert result["terminal_reason"] == "validation_exhausted"
    assert env["calls"]["execute_buy"] == 0

    # The audit trail must still capture Claude's analysis and the
    # guardrail's verdict on this path, even though it never reached
    # record_pending_approval (caught via review: finalize() originally
    # only backfilled dcf/technical/proposed_signal/sentiment_score).
    row = _log_row(env)
    assert row.status == "validation_exhausted"
    assert row.retry_count == 2
    assert row.llm_analysis_json is not None
    assert json.loads(row.llm_analysis_json)["cited_fair_value"] == -1.0
    assert row.validation_result_json is not None
    assert json.loads(row.validation_result_json)["valid"] is False


# --- Human approval / rejection ---

def test_human_approval_calls_execute_buy(env):
    result, config = _run(env)
    assert result["__interrupt__"]
    final = env["graph"].invoke(Command(resume={"decision": "approve"}), config=config)
    assert final["terminal_reason"] == "executed"
    assert env["calls"]["execute_buy"] == 1


def test_human_rejection_terminates_cleanly_without_trading(env):
    result, config = _run(env)
    assert result["__interrupt__"]
    final = env["graph"].invoke(Command(resume={"decision": "reject", "feedback": "not now"}), config=config)
    assert final["terminal_reason"] == "rejected"
    assert env["calls"]["execute_buy"] == 0

    row = _log_row(env)
    assert row.status == "rejected"
    assert row.human_decision == "reject"
    assert row.human_feedback == "not now"


# --- Missing/stale data cannot silently produce a trade ---

def test_missing_data_blocks_trade(env, monkeypatch):
    def broken_dcf(*a, **kw):
        raise RuntimeError("SEC EDGAR unavailable")

    monkeypatch.setattr(nodes, "run_dcf", broken_dcf)

    result, _ = _run(env)
    assert "__interrupt__" not in result
    assert result["terminal_reason"] == "engine_error"
    assert result["dcf_result"] is None
    assert env["calls"]["execute_buy"] == 0


# --- Deterministic gate rejection (evaluate_entry itself says no) ---

def test_deterministic_gate_rejects_downtrend(env, monkeypatch):
    def downtrend_technical(*a, **kw):
        bad = dict(TECH_OK)
        bad["trend"] = "down"
        return {"raw_output": "tech", "structured": bad}

    monkeypatch.setattr(nodes, "run_technical_scan", downtrend_technical)

    result, _ = _run(env)
    assert "__interrupt__" not in result
    assert result["terminal_reason"] == "no_signal"
    assert result["proposed_signal"] is None
    assert env["calls"]["execute_buy"] == 0

    # Audit trail must stay complete even on a no-signal path (caught via a
    # real dry run: finalize backfilled dcf/technical/proposed_signal but
    # originally forgot sentiment_score, leaving it None in the DB row).
    row = _log_row(env)
    assert row.sentiment_score == 0.5


# --- Risk-rule rejection at the execution boundary (never retried) ---

def test_risk_rejection_at_execution_is_not_retried(env, monkeypatch):
    def refuse(db, ticker, **kw):
        raise RiskRejected("daily loss limit breached")

    monkeypatch.setattr(nodes, "execute_buy", refuse)

    result, config = _run(env)
    assert result["__interrupt__"]
    final = env["graph"].invoke(Command(resume={"decision": "approve"}), config=config)
    assert final["terminal_reason"] == "execution_failed"
    assert "daily loss limit breached" in final["execution_error"]

    row = _log_row(env)
    assert row.status == "execution_failed"


# --- API layer: /graph/{ticker}/run and /graph/{thread_id}/resume over HTTP ---

def test_graph_api_endpoints(monkeypatch, tmp_path):
    # FastAPI runs sync route handlers in a worker thread, and sqlite's
    # `:memory:` DB is isolated per-connection/thread — a file-backed db
    # (matching test_execution_safety.py's own TestClient test) is required
    # here so the request thread and this test's assertions see the same data.
    from fastapi.testclient import TestClient
    from api import main

    api_engine = create_engine("sqlite:///" + str(tmp_path / "graph_api.db"))
    Base.metadata.create_all(api_engine)
    factory = sessionmaker(bind=api_engine)
    calls = {"execute_buy": 0}

    monkeypatch.setattr(nodes, "SessionLocal", factory)
    monkeypatch.setattr(nodes, "_latest_research", lambda db, ticker, module: None)
    monkeypatch.setattr(nodes, "_save_note", lambda db, ticker, module, result: None)
    monkeypatch.setattr(nodes, "get_company_name", lambda ticker: ticker)
    monkeypatch.setattr(nodes, "get_financial_summary", lambda ticker: "financials")
    monkeypatch.setattr(nodes, "fetch_ohlcv", lambda ticker: object())
    monkeypatch.setattr(nodes, "run_dcf", lambda *a, **kw: {"raw_output": "dcf", "structured": dict(DCF_OK)})
    monkeypatch.setattr(nodes, "run_technical_scan", lambda *a, **kw: {"raw_output": "tech", "structured": dict(TECH_OK)})
    monkeypatch.setattr(nodes, "_avg_sentiment", lambda db, ticker: 0.5)
    monkeypatch.setattr(nodes, "get_account", lambda: FakeAccount())
    monkeypatch.setattr(nodes, "run_llm_synthesis", lambda *a, **kw: {
        "raw_output": "analysis", "structured": dict(LLM_ANALYSIS_OK)})

    def fake_execute_buy(db, ticker, **kw):
        calls["execute_buy"] += 1
        return {"ticker": ticker, "action": "buy", "qty": 5, "price": 100.0,
            "order_id": "abc123", "status": "filled", "requested_qty": 5}

    monkeypatch.setattr(nodes, "execute_buy", fake_execute_buy)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "DASHBOARD_API_TOKEN", "test-api-token")
    client = TestClient(main.app)  # no `with`: startup workers intentionally not started
    headers = {"X-API-Token": "test-api-token"}

    started = client.post("/graph/AAPL/run", headers=headers)
    assert started.status_code == 200
    body = started.json()
    assert body["status"] == "pending_approval"
    assert body["ticker"] == "AAPL"
    thread_id = body["thread_id"]

    pending = client.get("/graph/pending", headers=headers).json()["pending"]
    assert any(p["thread_id"] == thread_id for p in pending)

    resumed = client.post(f"/graph/{thread_id}/resume", headers=headers, json={"decision": "approve"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "executed"
    assert calls["execute_buy"] == 1

    detail = client.get(f"/graph/{thread_id}", headers=headers).json()
    assert detail["status"] == "executed"
    assert detail["human_decision"] == "approve"

    stale = client.post(f"/graph/{thread_id}/resume", headers=headers, json={"decision": "approve"})
    assert stale.status_code == 409  # already resolved, nothing pending

    bad_decision = client.post("/graph/UNKNOWN123/resume", headers=headers, json={"decision": "maybe"})
    assert bad_decision.status_code == 422

    client.close()
    api_engine.dispose()

"""Typed state for the supervised LangGraph trade-decision workflow.

Deliberately excludes: the SQLAlchemy `db` session (not serializable, and
this graph pauses for human-timescale duration at the approval interrupt —
every node opens/closes its own short-lived session instead), the OHLCV
DataFrame (stays local to run_technical's stack), and any API key/secret
(none of the reused engine functions return one).
"""
from typing import Annotated, TypedDict
import operator


class GraphState(TypedDict):
    ticker: str
    thread_id: str

    dcf_result: dict | None
    technical_result: dict | None
    sentiment_score: float | None
    engine_errors: Annotated[list[str], operator.add]

    risk_snapshot: dict | None
    proposed_signal: dict | None

    llm_analysis: dict | None
    validation_result: dict | None
    validation_retry_count: int

    decision_log_id: int | None
    human_decision: str | None
    human_feedback: str | None

    execution_result: dict | None
    execution_error: str | None
    terminal_reason: str | None


def initial_state(ticker: str, thread_id: str) -> GraphState:
    return GraphState(
        ticker=ticker,
        thread_id=thread_id,
        dcf_result=None,
        technical_result=None,
        sentiment_score=None,
        engine_errors=[],
        risk_snapshot=None,
        proposed_signal=None,
        llm_analysis=None,
        validation_result=None,
        validation_retry_count=0,
        decision_log_id=None,
        human_decision=None,
        human_feedback=None,
        execution_result=None,
        execution_error=None,
        terminal_reason=None,
    )

"""Wires the nodes in agent/nodes.py into a LangGraph StateGraph.

START fans out to the three independent research nodes (they don't depend
on each other's output), which join at evaluate_signal — the deterministic
gate. From there: llm_synthesis <-> validate_synthesis is the only retry
loop, await_approval is the only interrupt, and execute_trade has exactly
one outgoing edge (finalize) regardless of outcome — order submission is
never retried by this graph.
"""
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    aggregate_sentiment,
    await_approval,
    evaluate_signal,
    execute_trade,
    finalize,
    llm_synthesis,
    record_pending_approval,
    route_after_approval,
    route_after_evaluate,
    route_after_validation,
    run_technical,
    run_valuation,
    validate_synthesis,
)
from agent.state import GraphState


def build_graph():
    builder = StateGraph(GraphState)

    builder.add_node("run_valuation", run_valuation)
    builder.add_node("run_technical", run_technical)
    builder.add_node("aggregate_sentiment", aggregate_sentiment)
    builder.add_node("evaluate_signal", evaluate_signal)
    builder.add_node("llm_synthesis", llm_synthesis)
    builder.add_node("validate_synthesis", validate_synthesis)
    builder.add_node("record_pending_approval", record_pending_approval)
    builder.add_node("await_approval", await_approval)
    builder.add_node("execute_trade", execute_trade)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "run_valuation")
    builder.add_edge(START, "run_technical")
    builder.add_edge(START, "aggregate_sentiment")
    builder.add_edge("run_valuation", "evaluate_signal")
    builder.add_edge("run_technical", "evaluate_signal")
    builder.add_edge("aggregate_sentiment", "evaluate_signal")

    builder.add_conditional_edges("evaluate_signal", route_after_evaluate, {
        "llm_synthesis": "llm_synthesis",
        "finalize": "finalize",
    })
    builder.add_edge("llm_synthesis", "validate_synthesis")
    builder.add_conditional_edges("validate_synthesis", route_after_validation, {
        "record_pending_approval": "record_pending_approval",
        "llm_synthesis": "llm_synthesis",
        "finalize": "finalize",
    })
    builder.add_edge("record_pending_approval", "await_approval")
    builder.add_conditional_edges("await_approval", route_after_approval, {
        "execute_trade": "execute_trade",
        "finalize": "finalize",
    })
    builder.add_edge("execute_trade", "finalize")
    builder.add_edge("finalize", END)

    return builder


_checkpointer = InMemorySaver()
_graph = build_graph().compile(checkpointer=_checkpointer)


def get_graph():
    return _graph

"""Offline close decisions from actual filled state; no independent position flag.

The five-field Phase 1 proxy is reproduced locally because importing its module
initializes an Anthropic client and reads environment configuration. Parity tests
bind this adapter to that original function without importing providers here.
"""
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import math
from types import MappingProxyType
from typing import Mapping

from engine.entry_exit.signals import evaluate_entry, evaluate_exit
from backtesting.primary_inputs import canonical_json, prepare_valuation
from backtesting.primary_ledger import PrimaryLedger
from backtesting.primary_state import (
    DecisionAction, DecisionRecord, EventTime, OrderIntent, Phase, PortfolioView,
    PreparedPrimaryData, PreparedValuation, PrimarySimulationConfig, Side,
    SimulationResult,
)


def _technical_proxy(row):
    """Exact Phase 1 proxy semantics; see module docstring for import boundary."""
    trend = "down" if row["close"] < row["sma_50"] else "up"
    return {
        "trend": trend,
        "entry_price": float(row["close"]),
        "stop_loss": float(row["support"]),
        "target_1": float(row["resistance"]),
        "probability_estimate": 55 if trend == "up" else 45,
    }


@dataclass(frozen=True)
class CloseObservation:
    symbol: str
    at: EventTime
    row: Mapping[str, float | None] | None
    valuation: PreparedValuation | None
    next_session: date | None
    input_id: str

    def __post_init__(self):
        if self.at.phase != Phase.CLOSE:
            raise ValueError("Observation requires a close")
        if self.next_session is not None and self.next_session <= self.at.session:
            raise ValueError("Execution must be in a later session")
        if self.valuation is not None:
            value = self.valuation.observation
            if value.symbol != self.symbol or value.effective_available_date > self.at.session:
                raise ValueError("Valuation unavailable at observation close")
        if self.row is not None:
            object.__setattr__(self, 'row', MappingProxyType(dict(self.row)))


class CloseInputs:
    """Only hand current rows to the evaluator, never a complete future frame."""
    def __init__(self, data: PreparedPrimaryData):
        self.data = data
        self._next = dict(zip(data.sessions, data.sessions[1:] + (None,)))
        self._rows = {(f.symbol, day): dict(zip(f.columns, row))
                      for f in data.frames for day, row in zip(f.dates, f.rows)}

    def at(self, symbol: str, at: EventTime) -> CloseObservation:
        if symbol not in self.data.symbols or at.session not in self._next:
            raise ValueError("Unknown symbol/session")
        row = self._rows.get((symbol, at.session))
        valuation = self.data.valuation_asof(symbol, at.session)
        # Local evidence hash, not full-dataset hash: future inputs cannot change
        # earlier decision identifiers. Full dataset identity stays in Result.
        digest = hashlib.sha256(canonical_json(dict(symbol=symbol, at=at, row=row,
            valuation=valuation, valuation_mode=self.data.valuation_mode)).encode()).hexdigest()
        return CloseObservation(symbol, at, row, valuation, self._next[at.session], digest)


def evaluate_close(symbol: str, observation: CloseObservation,
                   portfolio_view: PortfolioView, config: PrimarySimulationConfig) -> DecisionRecord:
    """Thin primary evaluator adapter; only successful future fills change state."""
    view, obs = portfolio_view, observation
    if obs.symbol != symbol or obs.at != view.at or view.at.phase != Phase.CLOSE:
        raise ValueError("Observation and portfolio close must match")
    position = view.positions.get(symbol)
    pending = view.pending.get(symbol)
    quantity = position.quantity if position else 0.

    def record(action, reason, intent=None):
        return DecisionRecord(view.at, symbol, action, reason, quantity, obs.input_id,
                              pending.order_id if pending else None, intent)

    if pending:
        return record(DecisionAction.BLOCKED, 'pending_order')
    if obs.row is None:
        return record(DecisionAction.BLOCKED, 'missing_close_bar')
    row = obs.row
    close = row.get('close')
    if close is None or not math.isfinite(close) or close <= 0:
        raise ValueError("Invalid prepared close")
    if position:
        signal = evaluate_exit(symbol, position.entry_price, close,
                               position.stop_price, position.target_price)
        if signal is None:
            return record(DecisionAction.HOLD, 'exit_gate_not_met')
        fields = dict(position_id=position.entry_order_id)
    else:
        if any(row.get(k) is None or not math.isfinite(row[k])
               for k in ('sma_50', 'support', 'resistance')):
            return record(DecisionAction.BLOCKED, 'indicator_warmup_or_missing')
        value = (prepare_valuation(obs.valuation.observation, config.valuation_mode)
                 if obs.valuation is not None else None)
        if value is None or not value.eligible:
            return record(DecisionAction.BLOCKED, 'valuation_unavailable')
        if view.equity <= 0:
            return record(DecisionAction.BLOCKED, 'invalid_decision_equity')
        technical = _technical_proxy(row)
        signal = evaluate_entry(symbol, {
            'estimated_fair_value_per_share': value.observation.fair_value,
            'share_count_source': value.observation.share_count_source,
        }, technical, 0., config.allocation_pct, view.equity)
        if signal is None:
            return record(DecisionAction.HOLD, 'entry_gate_not_met')
        fields = dict(allocation_pct=config.allocation_pct,
                      stop_price=technical['stop_loss'], target_price=technical['target_1'])
    side = Side(signal.action.value)
    intent = OrderIntent(f'{symbol}:{view.at.session.isoformat()}:{side.value}', symbol, side,
        view.at, obs.next_session, signal.reason, signal.confidence, view.equity,
        input_id=obs.input_id, **fields)
    return record(DecisionAction(side.value), signal.reason, intent)


def run_primary_strategy(data: PreparedPrimaryData,
                         config: PrimarySimulationConfig) -> SimulationResult:
    """One full chronological run, with decisions attached to the ledger result.

    No windows, external inputs, performance metrics or report schema here.
    The ledger remains the sole execution/accounting authority.
    """
    ledger = PrimaryLedger(data, config)  # also validates mode/calendar agreement
    inputs = CloseInputs(data)
    decisions = []
    for _ in data.sessions:
        ledger.open_session()
        view = ledger.close_session()
        # Held exits first, then flat entries; alphabetical within each group.
        symbols = sorted(data.symbols, key=lambda s: (s not in view.positions, s))
        batch = [evaluate_close(s, inputs.at(s, view.at), view, config) for s in symbols]
        ledger.submit_close(d.intent for d in batch if d.intent is not None)
        decisions.extend(batch)
    return replace(ledger.finish(), decisions=tuple(decisions))

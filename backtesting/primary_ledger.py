"""Offline primary shared-cash ledger, driven only by supplied close intents.

No entry/exit evaluator, DCF gate, intraday stops, CLI or performance reporting.
Opening sales settle immediately before alphabetical buys by explicit policy.
"""
from dataclasses import dataclass, replace
import math
from typing import Iterable, Mapping

from backtesting.primary_marking import PriceBook, has_stale_holdings, mark_portfolio
from backtesting.primary_state import (
    ClosedTrade, DecisionAction, DecisionRecord, EventTime, ExpirationReason,
    Fill, Mark, MarkQuality, OrderIntent, OrderOutcome, OrderStatus, Phase,
    PortfolioSnapshot, PortfolioState, PortfolioView, Position, PreparedPrimaryData,
    PrimarySimulationConfig, RejectionReason, Side, SimulationResult, number,
)


def size_entry(state: PortfolioState, intent: OrderIntent, open_price: float,
               config: PrimarySimulationConfig, at: EventTime) -> Fill | RejectionReason:
    """Cost-inclusive allocation. No mutation, borrowing, or fee double-counting."""
    number(open_price, 'opening price', positive=True)
    if intent.side != Side.BUY or intent.allocation_pct != config.allocation_pct:
        raise ValueError("Entry allocation must match configuration")
    if has_stale_holdings(state):
        return RejectionReason.STALE_PORTFOLIO_MARKS
    if not math.isfinite(state.equity) or state.equity <= 0:
        return RejectionReason.INVALID_EXECUTION_EQUITY
    if state.cash <= 0:
        return RejectionReason.INSUFFICIENT_CASH
    budget = config.allocation_pct * state.cash
    price = open_price * (1 + config.slippage_pct)
    divisor = price * (1 + config.fee_pct)
    number(divisor, 'cost per share', positive=True)
    quantity = budget / divisor
    number(quantity, "sized quantity")
    increment = config.quantity_increment
    if increment is not None:
        steps = quantity / increment
        if not math.isfinite(steps):
            raise ValueError("Unrepresentable quantity increment")
        quantity = math.floor(steps) * increment
    # Strict debit bound. Correct at most a few ulps, never clamp cash to zero.
    for _ in range(8):
        notional = quantity * price
        fee = notional * config.fee_pct
        debit = notional + fee
        if debit <= budget and debit <= state.cash:
            break
        quantity = max(0., quantity-increment) if increment else math.nextafter(quantity, 0.)
    else:
        raise ValueError("Cannot represent an affordable quantity")
    if quantity <= 0:
        return RejectionReason.QUANTITY_ROUNDS_TO_ZERO
    if notional < config.min_trade_notional:
        return RejectionReason.BELOW_MIN_NOTIONAL
    return Fill(intent.order_id, intent.symbol, Side.BUY, intent.signal_at, at,
                quantity, open_price, price, fee, quantity*(price-open_price), -debit)


def _advance(state, at):
    """Advance event sequence without changing price source timestamps."""
    return replace(state, at=at, positions={s: replace(p, mark=replace(p.mark, as_of=at))
        for s, p in state.positions.items()}, pending=dict(state.pending))


def assert_invariants(state: PortfolioState, submitted: Mapping[str, OrderIntent],
                      book: PriceBook, config: PrimarySimulationConfig) -> None:
    """Reconstruct cash, positions and realized economics from terminal records."""
    state.validate()
    tolerance = 1e-8 * max(1., state.initial_cash)

    def equal(a, b, label):
        if not math.isclose(a, b, rel_tol=0., abs_tol=tolerance):
            raise ValueError(f'Ledger invariant: {label}')

    terminal_ids = {o.order_id for o in state.outcomes}
    pending_ids = {o.order_id for o in state.pending.values()}
    if set(submitted) != terminal_ids | pending_ids or terminal_ids & pending_ids:
        raise ValueError("Submitted orders must have one pending or terminal record")
    cash, net, gross, fees, slip = state.initial_cash, 0., 0., 0., 0.
    holdings = {}
    prior_at = None
    for outcome in state.outcomes:
        if prior_at is not None and outcome.at <= prior_at:
            raise ValueError("Terminal events must be strictly chronological")
        prior_at = outcome.at
        intent = submitted[outcome.order_id]
        if outcome.at < intent.signal_at:
            raise ValueError("Outcome before signal")
        if outcome.status != OrderStatus.FILLED:
            if outcome.cash_delta != 0:
                raise ValueError("Nonfill moved cash")
            continue
        fill = outcome.fill
        if (fill.symbol != intent.symbol or fill.side != intent.side or fill.signal_at != intent.signal_at
                or fill.at.session != intent.eligible_session):
            raise ValueError("Fill does not match originating intent")
        opening = book.price(fill.symbol, fill.at)
        if opening is None or fill.reference_price != opening:
            raise ValueError("Fill did not use current opening price")
        expected_price = opening*(1+config.slippage_pct if fill.side == Side.BUY else 1-config.slippage_pct)
        equal(fill.price, expected_price, 'execution price')
        equal(fill.fee, fill.quantity*fill.price*config.fee_pct, 'fee')
        if fill.side == Side.BUY:
            if fill.symbol in holdings or -fill.cash_delta > cash:
                raise ValueError("Duplicate holding or historical cash overspend")
            if -fill.cash_delta > cash*config.allocation_pct:
                raise ValueError("Historical allocation exceeded")
            holdings[fill.symbol] = fill
        else:
            entry = holdings.pop(fill.symbol, None)
            if entry is None or intent.position_id != entry.order_id or fill.quantity != entry.quantity:
                raise ValueError("Orphan or partial exit")
            net += fill.cash_delta + entry.cash_delta
            gross += fill.quantity*(fill.reference_price-entry.reference_price)
        cash += fill.cash_delta
        fees += fill.fee
        slip += fill.slippage_cost
    if set(holdings) != set(state.positions):
        raise ValueError("Filled holdings do not match state")
    for symbol, entry in holdings.items():
        pos = state.positions[symbol]
        intent = submitted[entry.order_id]
        if (pos.entry_order_id != entry.order_id or pos.quantity != entry.quantity or
                pos.entry_at != entry.at or pos.signal_at != entry.signal_at or
                pos.stop_price != intent.stop_price or pos.target_price != intent.target_price):
            raise ValueError("Position metadata differs from accepted entry")
        equal(pos.cost_basis, -entry.cash_delta, 'cost basis')
        equal(pos.entry_price, entry.price, 'entry price')
        equal(pos.reference_entry_price, entry.reference_price, 'reference price')
        equal(pos.entry_fee, entry.fee, 'entry fee')
        source = book.price(symbol, pos.mark.observed_at)
        if source is None or source != pos.mark.value:
            raise ValueError("Mark lacks a historical price source")
        current = book.price(symbol, state.at)
        if (pos.mark.quality == MarkQuality.CURRENT) != (current is not None):
            raise ValueError("Stale/current mark classification mismatch")
    equal(cash, state.cash, 'cash movements')
    equal(net, state.realized_net_pnl, 'realized net P&L')
    equal(gross, state.realized_gross_pnl, 'realized gross P&L')
    equal(fees, state.fees_paid, 'fee totals')
    equal(slip, state.slippage_costs, 'slippage totals')
    unrealized_gross = sum(p.quantity*(p.mark.value-p.reference_entry_price) for p in state.positions.values())
    equal(gross+unrealized_gross-fees-slip, state.equity-state.initial_cash, 'total economics')


@dataclass(frozen=True)
class LedgerEvent:
    kind: str
    snapshot: PortfolioSnapshot
    positions: tuple[Position, ...]
    pending_ids: tuple[str, ...]
    order_id: str | None = None


class PrimaryLedger:
    """Explicit session driver. Public views/history cannot mutate internal state."""

    def __init__(self, data: PreparedPrimaryData, config: PrimarySimulationConfig):
        if data.valuation_mode != config.valuation_mode or data.metadata['calendar_source'] != config.calendar_policy:
            raise ValueError("Prepared data and config policies differ")
        self.data, self.config = data, config
        self._book = PriceBook(data)
        self._state = PortfolioState(EventTime(data.sessions[0], Phase.OPEN), config.initial_cash,
                                     config.initial_cash, config.initial_cash)
        self._submitted = {}
        self._events = []
        self._decisions = []
        self._index = -1
        self._stage = 'ready'
        self._close_submitted = False
        self._history = []
        assert_invariants(self._state, self._submitted, self._book, config)

    @property
    def state(self):
        return replace(self._state, positions=dict(self._state.positions), pending=dict(self._state.pending))

    @property
    def events(self):
        return tuple(self._events)

    @property
    def submitted(self):
        from types import MappingProxyType
        return MappingProxyType(dict(self._submitted))

    def _next_at(self):
        return replace(self._state.at, sequence=self._state.at.sequence+1)

    def _publish(self, candidate, kind, order_id=None, submitted=None):
        registry = self._submitted if submitted is None else submitted
        candidate.next_sequence = candidate.at.sequence+1
        assert_invariants(candidate, registry, self._book, self.config)
        snapshot = PortfolioSnapshot(candidate.at, candidate.cash, candidate.equity, candidate.exposure,
                                     tuple((s, p.mark) for s, p in sorted(candidate.positions.items())))
        event = LedgerEvent(kind, snapshot, tuple(p for _, p in sorted(candidate.positions.items())),
                            tuple(sorted(o.order_id for o in candidate.pending.values())), order_id)
        self._state, self._submitted = candidate, dict(registry)
        self._events.append(event)
        self._history.append(snapshot)

    def open_session(self) -> PortfolioView:
        if self._stage not in ('ready', 'close') or self._index+1 >= len(self.data.sessions):
            raise ValueError("No next session or previous session not closed")
        next_index = self._index+1
        at = EventTime(self.data.sessions[next_index], Phase.OPEN)
        marked = mark_portfolio(self._state, self._book, at)
        self._publish(marked, 'opening_marks')
        self._index, self._stage = next_index, 'open'
        orders = sorted(self._state.pending.values(), key=lambda o: (o.side == Side.BUY, o.symbol, o.order_id))
        for intent in orders:
            if intent.eligible_session != at.session:
                raise ValueError("Pending intent missed its permitted opening")
            opening = self._book.price(intent.symbol, at)
            if opening is None:
                self._nonfill(intent, OrderStatus.EXPIRED, ExpirationReason.MISSING_EXECUTION_BAR)
                continue
            outcome = self._propose(intent, opening, self._next_at())
            if isinstance(outcome, RejectionReason):
                self._nonfill(intent, OrderStatus.REJECTED, outcome)
            else:
                self.apply_fill(outcome)
        return self._state.decision_view()

    def _propose(self, intent, opening, at):
        if intent.side == Side.BUY:
            return size_entry(self._state, intent, opening, self.config, at)
        pos = self._state.positions.get(intent.symbol)
        if pos is None or pos.entry_order_id != intent.position_id:
            raise ValueError("Sell does not reference a held position")
        price = opening*(1-self.config.slippage_pct)
        fee = pos.quantity*price*self.config.fee_pct
        return Fill(intent.order_id, intent.symbol, Side.SELL, intent.signal_at, at,
                    pos.quantity, opening, price, fee, pos.quantity*(opening-price), pos.quantity*price-fee)

    def apply_fill(self, fill: Fill) -> None:
        """Validate against the exact pending proposal, then publish atomically.

        Partial exits or manually changed quantities/prices cannot bypass sizing.
        """
        if self._stage != 'open':
            raise ValueError("Fills are permitted only during opening execution")
        intent = self._state.pending.get(fill.symbol)
        if intent is None or intent.order_id != fill.order_id or intent.eligible_session != self._state.at.session:
            raise ValueError("Fill has no eligible pending intent")
        expected = self._propose(intent, self._book.price(intent.symbol, self._state.at), self._next_at())
        if not isinstance(expected, Fill) or fill != expected:
            raise ValueError("Fill differs from approved full proposal (partial fills unsupported)")
        candidate = _advance(self._state, fill.at)
        del candidate.pending[intent.symbol]
        if fill.side == Side.BUY:
            if intent.symbol in candidate.positions:
                raise ValueError("Duplicate position")
            candidate.positions[intent.symbol] = Position(intent.symbol, fill.quantity, intent.order_id,
                intent.signal_at, fill.at, fill.reference_price, fill.price, fill.fee, -fill.cash_delta,
                intent.stop_price, intent.target_price,
                Mark(fill.reference_price, fill.at, fill.at, MarkQuality.CURRENT))
        else:
            pos = candidate.positions.pop(intent.symbol)
            candidate.realized_net_pnl += fill.cash_delta-pos.cost_basis
            candidate.realized_gross_pnl += fill.quantity*(fill.reference_price-pos.reference_entry_price)
        candidate.cash += fill.cash_delta
        candidate.fees_paid += fill.fee
        candidate.slippage_costs += fill.slippage_cost
        candidate.equity = candidate.cash+candidate.exposure
        candidate.outcomes += (OrderOutcome(intent.order_id, OrderStatus.FILLED, fill.at, fill=fill),)
        self._publish(candidate, 'fill', intent.order_id)

    def _nonfill(self, intent, status, reason):
        candidate = _advance(self._state, self._next_at())
        del candidate.pending[intent.symbol]
        candidate.outcomes += (OrderOutcome(intent.order_id, status, candidate.at, reason),)
        self._publish(candidate, status.value, intent.order_id)

    def close_session(self) -> PortfolioView:
        if self._stage != 'open':
            raise ValueError("Session must be open before closing")
        at = EventTime(self.data.sessions[self._index], Phase.CLOSE)
        self._publish(mark_portfolio(self._state, self._book, at), 'closing_marks')
        self._stage, self._close_submitted = 'close', False
        return self._state.decision_view()

    def submit_close(self, intents: Iterable[OrderIntent]) -> None:
        """One atomic batch per close; synthetic callers use close_session's view."""
        if self._stage != 'close' or self._close_submitted:
            raise ValueError("Submit once at the current close")
        batch = sorted(tuple(intents), key=lambda o: (o.symbol, o.order_id))
        registry = dict(self._submitted)
        candidate = _advance(self._state, self._next_at())
        next_day = self.data.sessions[self._index+1] if self._index+1 < len(self.data.sessions) else None
        decisions = []
        for intent in batch:
            if intent.symbol not in self.data.symbols or intent.signal_at != self._state.at:
                raise ValueError("Intent must originate at the current close for a known symbol")
            if intent.order_id in registry or intent.symbol in candidate.pending:
                raise ValueError("Duplicate order ID or contradictory pending symbol")
            if intent.eligible_session != next_day:
                raise ValueError("Intent must target exactly the next canonical session")
            if intent.decision_equity != self._state.equity:
                raise ValueError("Intent equity must match the observed closing snapshot")
            if intent.side == Side.BUY and intent.allocation_pct != self.config.allocation_pct:
                raise ValueError("Intent allocation differs from configuration")
            registry[intent.order_id] = intent
            candidate.pending[intent.symbol] = intent
            decisions.append(DecisionRecord(intent.signal_at, intent.symbol, DecisionAction(intent.side.value),
                intent.reason, self._state.positions[intent.symbol].quantity if intent.symbol in self._state.positions else 0.,
                intent.input_id, intent=intent))
        self._publish(candidate, 'pending_update', submitted=registry)
        self._decisions.extend(decisions)
        self._close_submitted = True

    def finish(self, expiration_reason=ExpirationReason.END_OF_DATASET) -> SimulationResult:
        if self._stage != 'close' or self._index != len(self.data.sessions)-1:
            raise ValueError("Finish only after the final session close")
        if expiration_reason not in (ExpirationReason.END_OF_DATASET, ExpirationReason.END_OF_WINDOW):
            raise ValueError("Unsupported terminal expiration reason")
        for intent in sorted(self._state.pending.values(), key=lambda o: (o.symbol, o.order_id)):
            self._nonfill(intent, OrderStatus.EXPIRED, expiration_reason)
        self._stage = 'finished'
        return self.result()

    def result(self) -> SimulationResult:
        if self._stage != 'finished':
            raise ValueError("Result requires terminal expiration via finish()")
        assert_invariants(self._state, self._submitted, self._book, self.config)
        fills = tuple(o.fill for o in self._state.outcomes if o.fill is not None)
        entries, trades = {}, []
        for fill in fills:
            if fill.side == Side.BUY:
                entries[fill.symbol] = fill
            else:
                trades.append(ClosedTrade(fill.symbol, entries.pop(fill.symbol), fill))
        return SimulationResult(self.config, self.data.content_hash, fills=fills,
            outcomes=self._state.outcomes, decisions=tuple(self._decisions),
            terminal_positions=tuple(p for _, p in sorted(self._state.positions.items())),
            history=tuple(self._history), closed_trades=tuple(trades), manifest_json=self.data.metadata_json)


def simulate_primary(data: PreparedPrimaryData, config: PrimarySimulationConfig,
                     intents: Iterable[OrderIntent] = ()) -> SimulationResult:
    """Replay fixed synthetic intents; no strategy callbacks or future lifecycle scan."""
    by_day = {}
    for intent in intents:
        if intent.signal_at.session not in data.sessions:
            raise ValueError("Signal outside canonical dataset")
        by_day.setdefault(intent.signal_at.session, []).append(intent)
    ledger = PrimaryLedger(data, config)
    for day in data.sessions:
        ledger.open_session()
        ledger.close_session()
        ledger.submit_close(by_day.get(day, ()))
    return ledger.finish()

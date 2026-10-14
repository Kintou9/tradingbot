"""Offline primary shared-cash ledger, driven only by supplied close intents.

No entry/exit evaluator, DCF gate, intraday stops, CLI or performance reporting.
Opening sales settle immediately before alphabetical buys by explicit policy.
"""
from dataclasses import dataclass, replace
import math
from types import MappingProxyType
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


@dataclass(frozen=True)
class _JournalNode:
    """One immutable node in the ledger-owned authoritative outcome journal."""
    outcome: OrderOutcome
    previous: "_JournalNode | None" = None


@dataclass(frozen=True)
class _OutcomeJournal:
    """Append-efficient authority; public/state tuples are derived views only.

    Incremental validation protects publication; the independent terminal
    reconciler audits the materialized journal before a result is returned.
    """
    head: _JournalNode | None = None
    size: int = 0
    fill_count: int = 0

    def appended(self, outcome: OrderOutcome) -> "_OutcomeJournal":
        if not isinstance(outcome, OrderOutcome):
            raise TypeError("Journal accepts only terminal order outcomes")
        return _OutcomeJournal(_JournalNode(outcome, self.head), self.size + 1,
                               self.fill_count + (outcome.fill is not None))

    def view(self) -> tuple[OrderOutcome, ...]:
        values = []
        node = self.head
        while node is not None:
            values.append(node.outcome)
            node = node.previous
        if len(values) != self.size:
            raise ValueError("Outcome journal size mismatch")
        values.reverse()
        return tuple(values)


@dataclass(frozen=True)
class _HistoryNode:
    value: object
    previous: "_HistoryNode | None" = None


@dataclass(frozen=True)
class _PersistentHistory:
    """Immutable reverse-linked history with O(1) structural-share append."""
    head: _HistoryNode | None = None
    size: int = 0

    def appended(self, value) -> "_PersistentHistory":
        return _PersistentHistory(_HistoryNode(value, self.head), self.size + 1)

    def extended(self, values) -> "_PersistentHistory":
        result = self
        for value in values:
            result = result.appended(value)
        return result

    def view(self) -> tuple:
        values = []
        node = self.head
        while node is not None:
            values.append(node.value)
            node = node.previous
        if len(values) != self.size:
            raise ValueError("Persistent history size mismatch")
        values.reverse()
        return tuple(values)


@dataclass(frozen=True)
class _ValidationMetadata:
    outcome_count: int = 0
    fill_count: int = 0
    cumulative_cash_delta: float = 0.0
    last_outcome_at: EventTime | None = None


@dataclass(frozen=True)
class _LedgerGeneration:
    """One coherent published ledger generation; replaced by one assignment.

    PortfolioState is privately owned and its collection fields are read-only
    mapping proxies. No published state object is mutated after construction.
    """
    state: PortfolioState
    journal: _OutcomeJournal
    submitted: Mapping[str, OrderIntent]
    events: _PersistentHistory
    history: _PersistentHistory
    decisions: _PersistentHistory
    validation: _ValidationMetadata
    index: int
    stage: str
    close_submitted: bool
    terminal_reason: ExpirationReason | None = None


def _published_state(state: PortfolioState) -> PortfolioState:
    return replace(state, positions=MappingProxyType(dict(state.positions)),
                   pending=MappingProxyType(dict(state.pending)), outcomes=tuple(state.outcomes))


def validate_transition(current: _LedgerGeneration, candidate: PortfolioState, kind: str,
                        registry: Mapping[str, OrderIntent], book: PriceBook,
                        config: PrimarySimulationConfig, *, outcome: OrderOutcome | None,
                        decisions=(), index=None, stage=None,
                        close_submitted=None) -> _ValidationMetadata:
    """Independently validate one bounded transition without replaying history.

    This arithmetic intentionally does not call `assert_invariants`, sizing,
    fill proposal, or marking helpers. The future terminal reconciler must remain
    a third, independently implemented accounting path.
    """
    previous = current.state
    tolerance = 1e-8 * max(1., previous.initial_cash)
    prior_meta = current.validation
    if (prior_meta.outcome_count != current.journal.size or
            prior_meta.fill_count != current.journal.fill_count or
            prior_meta.outcome_count < 0 or prior_meta.fill_count < 0 or
            prior_meta.fill_count > prior_meta.outcome_count or
            (prior_meta.outcome_count == 0) != (prior_meta.last_outcome_at is None) or
            not math.isfinite(prior_meta.cumulative_cash_delta) or
            not math.isclose(previous.cash, previous.initial_cash+prior_meta.cumulative_cash_delta,
                             rel_tol=0., abs_tol=1e-8*max(1., previous.initial_cash))):
        raise ValueError('Incremental invariant: validation metadata mismatch')
    next_index = current.index if index is None else index
    next_stage = current.stage if stage is None else stage
    next_close_submitted = current.close_submitted if close_submitted is None else close_submitted

    def equal(left, right, label):
        if not math.isfinite(left) or not math.isfinite(right) or not math.isclose(
                left, right, rel_tol=0., abs_tol=tolerance):
            raise ValueError('Incremental invariant: ' + label)

    def same_finances(*, cash=True, realized=True, costs=True):
        if cash:
            equal(candidate.cash, previous.cash, 'cash changed')
        if realized:
            equal(candidate.realized_net_pnl, previous.realized_net_pnl, 'realized net changed')
            equal(candidate.realized_gross_pnl, previous.realized_gross_pnl, 'realized gross changed')
        if costs:
            equal(candidate.fees_paid, previous.fees_paid, 'fees changed')
            equal(candidate.slippage_costs, previous.slippage_costs, 'slippage changed')

    def same_positions(*, allow_marks=False):
        if set(candidate.positions) != set(previous.positions):
            raise ValueError('Incremental invariant: positions changed')
        for symbol, old in previous.positions.items():
            new = candidate.positions[symbol]
            if (replace(new, mark=old.mark) if allow_marks else new) != old:
                raise ValueError('Incremental invariant: position economics changed')

    def advanced(position):
        return replace(position, mark=replace(position.mark, as_of=candidate.at))

    expected_index, expected_stage, expected_close = current.index, current.stage, current.close_submitted
    if kind == 'opening_marks':
        if current.stage not in ('ready', 'close'):
            raise ValueError('Incremental invariant: invalid opening lifecycle')
        expected_index, expected_stage = current.index + 1, 'open'
    elif kind == 'closing_marks':
        if current.stage != 'open':
            raise ValueError('Incremental invariant: invalid closing lifecycle')
        expected_stage, expected_close = 'close', False
    elif kind == 'pending_update':
        if current.stage != 'close' or current.close_submitted:
            raise ValueError('Incremental invariant: invalid submission lifecycle')
        expected_close = True
    elif kind in ('fill', 'rejected', 'expired'):
        if current.stage not in ('open', 'close'):
            raise ValueError('Incremental invariant: invalid outcome lifecycle')
    else:
        raise ValueError('Incremental invariant: unknown transition kind')
    if (next_index, next_stage, next_close_submitted) != (expected_index, expected_stage, expected_close):
        raise ValueError('Incremental invariant: lifecycle metadata mismatch')
    initial_open = kind == 'opening_marks' and current.index == -1 and candidate.at == previous.at
    if candidate.at <= previous.at and not initial_open:
        raise ValueError('Incremental invariant: event time did not advance')

    if kind in ('opening_marks', 'closing_marks'):
        if outcome is not None or registry is not current.submitted:
            raise ValueError('Incremental invariant: mark fabricated an order change')
        same_finances()
        same_positions(allow_marks=True)
        if candidate.pending != previous.pending:
            raise ValueError('Incremental invariant: mark changed pending orders')
        phase = Phase.OPEN if kind == 'opening_marks' else Phase.CLOSE
        if candidate.at.phase != phase or candidate.at.session != book.sessions[next_index]:
            raise ValueError('Incremental invariant: mark time mismatch')
        for symbol, old in previous.positions.items():
            new_mark = candidate.positions[symbol].mark
            observed = book.price(symbol, candidate.at)
            if observed is None:
                if (new_mark.value != old.mark.value or new_mark.observed_at != old.mark.observed_at or
                        new_mark.quality != MarkQuality.STALE):
                    raise ValueError('Incremental invariant: invalid stale mark')
            elif (new_mark.value != observed or new_mark.observed_at != candidate.at or
                  new_mark.quality != MarkQuality.CURRENT):
                raise ValueError('Incremental invariant: incorrect current mark')
    elif kind == 'pending_update':
        if outcome is not None:
            raise ValueError('Incremental invariant: submission created outcome')
        same_finances()
        if candidate.positions != {symbol: advanced(position) for symbol, position in previous.positions.items()}:
            raise ValueError('Incremental invariant: submission changed positions')
        batch = tuple(decisions)
        if len(registry) != len(current.submitted) + len(batch):
            raise ValueError('Incremental invariant: submitted registry count')
        expected_pending = dict(previous.pending)
        for decision in batch:
            intent = decision.intent
            if intent is None or registry.get(intent.order_id) != intent or intent.symbol in expected_pending:
                raise ValueError('Incremental invariant: submitted intent mismatch')
            expected_pending[intent.symbol] = intent
        if candidate.pending != expected_pending:
            raise ValueError('Incremental invariant: pending batch mismatch')
    else:
        if outcome is None or outcome.order_id not in registry:
            raise ValueError('Incremental invariant: missing registered outcome')
        intent = registry[outcome.order_id]
        pending = previous.pending.get(intent.symbol)
        if pending != intent or outcome.at <= (current.validation.last_outcome_at or intent.signal_at):
            raise ValueError('Incremental invariant: pending/outcome chronology mismatch')
        expected_pending = dict(previous.pending)
        del expected_pending[intent.symbol]
        if candidate.pending != expected_pending:
            raise ValueError('Incremental invariant: terminal order did not clear pending')
        expected_kind = 'fill' if outcome.status == OrderStatus.FILLED else outcome.status.value
        if kind != expected_kind:
            raise ValueError('Incremental invariant: outcome status mismatch')
        if outcome.fill is None:
            same_finances()
            if candidate.positions != {symbol: advanced(position) for symbol, position in previous.positions.items()}:
                raise ValueError('Incremental invariant: nonfill changed positions')
            if kind == 'rejected':
                if not isinstance(outcome.reason, RejectionReason) or current.stage != 'open':
                    raise ValueError('Incremental invariant: invalid rejection')
            else:
                if not isinstance(outcome.reason, ExpirationReason):
                    raise ValueError('Incremental invariant: invalid expiration')
                if outcome.reason == ExpirationReason.MISSING_EXECUTION_BAR:
                    if current.stage != 'open' or book.price(intent.symbol, previous.at) is not None:
                        raise ValueError('Incremental invariant: invalid missing-bar expiration')
                elif current.stage != 'close' or current.index != len(book.sessions)-1:
                    raise ValueError('Incremental invariant: invalid terminal expiration')
        else:
            fill = outcome.fill
            if (kind != 'fill' or fill.order_id != intent.order_id or
                    fill.symbol != intent.symbol or fill.side != intent.side or
                    fill.signal_at != intent.signal_at):
                raise ValueError('Incremental invariant: fill identity mismatch')
            opening = book.price(fill.symbol, fill.at)
            if opening is None or fill.at.session != intent.eligible_session:
                raise ValueError('Incremental invariant: fill execution source')
            if fill.reference_price != opening:
                raise ValueError('Incremental invariant: fill reference price')
            expected_price = opening * (1 + config.slippage_pct if fill.side == Side.BUY else 1 - config.slippage_pct)
            expected_fee = fill.quantity * expected_price * config.fee_pct
            expected_slip = fill.quantity * abs(expected_price-opening)
            equal(fill.price, expected_price, 'execution price')
            equal(fill.fee, expected_fee, 'fill fee')
            equal(fill.slippage_cost, expected_slip, 'fill slippage')
            expected_cash_delta = -(fill.quantity*expected_price+expected_fee) if fill.side == Side.BUY else fill.quantity*expected_price-expected_fee
            equal(fill.cash_delta, expected_cash_delta, 'fill cash delta')
            equal(candidate.cash, previous.cash+expected_cash_delta, 'post-fill cash')
            equal(candidate.fees_paid, previous.fees_paid+expected_fee, 'post-fill fees')
            equal(candidate.slippage_costs, previous.slippage_costs+expected_slip, 'post-fill slippage')
            if candidate.cash < -tolerance:
                raise ValueError('Incremental invariant: negative cash')
            if fill.side == Side.BUY:
                if -expected_cash_delta > previous.cash * config.allocation_pct + tolerance:
                    raise ValueError('Incremental invariant: buy allocation exceeded')
                if intent.symbol in previous.positions or set(candidate.positions) != set(previous.positions) | {intent.symbol}:
                    raise ValueError('Incremental invariant: buy position set')
                for symbol, position in previous.positions.items():
                    if candidate.positions[symbol] != advanced(position):
                        raise ValueError('Incremental invariant: buy changed another position')
                position = candidate.positions[intent.symbol]
                if (position.quantity != fill.quantity or position.entry_order_id != fill.order_id or
                        position.cost_basis != -expected_cash_delta or position.entry_price != expected_price or
                        position.reference_entry_price != opening or position.entry_fee != expected_fee or
                        position.stop_price != intent.stop_price or position.target_price != intent.target_price):
                    raise ValueError('Incremental invariant: accepted buy position')
                equal(candidate.realized_net_pnl, previous.realized_net_pnl, 'buy realized net')
                equal(candidate.realized_gross_pnl, previous.realized_gross_pnl, 'buy realized gross')
            else:
                held = previous.positions.get(intent.symbol)
                if held is None or intent.position_id != held.entry_order_id or fill.quantity != held.quantity:
                    raise ValueError('Incremental invariant: sell holding mismatch')
                expected_positions = {symbol: advanced(position) for symbol, position in previous.positions.items()}
                del expected_positions[intent.symbol]
                if candidate.positions != expected_positions:
                    raise ValueError('Incremental invariant: sell position set')
                equal(candidate.realized_net_pnl, previous.realized_net_pnl+expected_cash_delta-held.cost_basis,
                      'sell realized net')
                equal(candidate.realized_gross_pnl, previous.realized_gross_pnl+fill.quantity*(opening-held.reference_entry_price),
                      'sell realized gross')

    exposure = sum(position.quantity*position.mark.value for position in candidate.positions.values())
    equal(candidate.equity, candidate.cash+exposure, 'equity')
    # Snapshot-local validation is bounded by current positions/pending orders;
    # it does not inspect the persistent historical journal.
    candidate.validate()
    metadata = current.validation
    if outcome is None:
        return metadata
    cash_total = metadata.cumulative_cash_delta + outcome.cash_delta
    equal(candidate.cash, candidate.initial_cash+cash_total, 'cumulative cash delta')
    return _ValidationMetadata(metadata.outcome_count+1,
        metadata.fill_count + (outcome.fill is not None), cash_total, outcome.at)


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
        state = _published_state(PortfolioState(EventTime(data.sessions[0], Phase.OPEN),
                                 config.initial_cash, config.initial_cash, config.initial_cash))
        assert_invariants(state, {}, self._book, config)
        self._generation = _LedgerGeneration(state, _OutcomeJournal(), MappingProxyType({}),
            _PersistentHistory(), _PersistentHistory(), _PersistentHistory(),
            _ValidationMetadata(), -1, 'ready', False)

    @property
    def _state(self):
        return self._generation.state

    @property
    def _journal(self):
        return self._generation.journal

    @property
    def _submitted(self):
        return self._generation.submitted

    @property
    def _events(self):
        return self._generation.events

    @property
    def _history(self):
        return self._generation.history

    @property
    def _decisions(self):
        return self._generation.decisions

    @property
    def _index(self):
        return self._generation.index

    @property
    def _stage(self):
        return self._generation.stage

    @property
    def _close_submitted(self):
        return self._generation.close_submitted

    @property
    def state(self):
        return replace(self._state, outcomes=self._journal.view(),
                       positions=dict(self._state.positions), pending=dict(self._state.pending))

    @property
    def events(self):
        return self._events.view()

    @property
    def outcomes(self):
        """Immutable chronological view derived from the ledger-owned journal."""
        return self._journal.view()

    @property
    def submitted(self):
        from types import MappingProxyType
        return MappingProxyType(dict(self._submitted))

    def _next_at(self):
        return replace(self._state.at, sequence=self._state.at.sequence+1)

    def _before_publication(self, generation):
        """Fault-injection boundary after preparation and before reference swaps."""

    def _publish(self, candidate, kind, order_id=None, submitted=None, outcome=None,
                 decisions=(), index=None, stage=None, close_submitted=None):
        current = self._generation
        registry = self._submitted if submitted is None else submitted
        metadata = validate_transition(current, candidate, kind, registry, self._book,
            self.config, outcome=outcome, decisions=decisions, index=index, stage=stage,
            close_submitted=close_submitted)
        journal = self._journal.appended(outcome) if outcome is not None else self._journal
        # The persistent journal is authoritative. Do not materialize its
        # accumulated history on the publication path.
        candidate = replace(candidate, outcomes=())
        candidate.next_sequence = candidate.at.sequence+1
        snapshot = PortfolioSnapshot(candidate.at, candidate.cash, candidate.equity, candidate.exposure,
                                     tuple((s, p.mark) for s, p in sorted(candidate.positions.items())))
        event = LedgerEvent(kind, snapshot, tuple(p for _, p in sorted(candidate.positions.items())),
                            tuple(sorted(o.order_id for o in candidate.pending.values())), order_id)
        prepared = _LedgerGeneration(
            _published_state(candidate), journal,
            current.submitted if submitted is None else MappingProxyType(dict(registry)),
            self._events.appended(event), self._history.appended(snapshot),
            self._decisions.extended(tuple(decisions)),
            metadata,
            current.index if index is None else index,
            current.stage if stage is None else stage,
            current.close_submitted if close_submitted is None else close_submitted,
        )
        self._before_publication(prepared)
        self._generation = prepared

    def open_session(self) -> PortfolioView:
        if self._stage not in ('ready', 'close') or self._index+1 >= len(self.data.sessions):
            raise ValueError("No next session or previous session not closed")
        next_index = self._index+1
        at = EventTime(self.data.sessions[next_index], Phase.OPEN)
        marked = mark_portfolio(self._state, self._book, at)
        self._publish(marked, 'opening_marks', index=next_index, stage='open')
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
        outcome = OrderOutcome(intent.order_id, OrderStatus.FILLED, fill.at, fill=fill)
        self._publish(candidate, 'fill', intent.order_id, outcome=outcome)

    def _nonfill(self, intent, status, reason):
        candidate = _advance(self._state, self._next_at())
        del candidate.pending[intent.symbol]
        outcome = OrderOutcome(intent.order_id, status, candidate.at, reason)
        self._publish(candidate, status.value, intent.order_id, outcome=outcome)

    def close_session(self) -> PortfolioView:
        if self._stage != 'open':
            raise ValueError("Session must be open before closing")
        at = EventTime(self.data.sessions[self._index], Phase.CLOSE)
        self._publish(mark_portfolio(self._state, self._book, at), 'closing_marks',
                      stage='close', close_submitted=False)
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
        self._publish(candidate, 'pending_update', submitted=registry, decisions=decisions,
                      close_submitted=True)

    def finish(self, expiration_reason=ExpirationReason.END_OF_DATASET) -> SimulationResult:
        if self._stage != 'close' or self._index != len(self.data.sessions)-1:
            raise ValueError("Finish only after the final session close")
        if expiration_reason not in (ExpirationReason.END_OF_DATASET, ExpirationReason.END_OF_WINDOW):
            raise ValueError("Unsupported terminal expiration reason")
        for intent in sorted(self._state.pending.values(), key=lambda o: (o.symbol, o.order_id)):
            self._nonfill(intent, OrderStatus.EXPIRED, expiration_reason)
        current = self._generation
        prepared = replace(current, stage='finished', terminal_reason=expiration_reason)
        self._before_publication(prepared)
        self._generation = prepared
        return self.result()

    def result(self) -> SimulationResult:
        if self._stage != 'finished':
            raise ValueError("Result requires terminal expiration via finish()")
        outcomes = self._journal.view()
        state = replace(self._state, outcomes=outcomes)
        from backtesting.primary_reconciliation import reconcile_terminal
        reconcile_terminal(config=self.config, book=self._book, submitted=self._submitted,
            outcomes=outcomes, events=self.events, history=self._history.view(),
            terminal_state=state, terminal_reason=self._generation.terminal_reason)
        from backtesting.primary_audit_receipt import build_audit_evidence, create_audit_receipt
        evidence = build_audit_evidence(dataset_hash=self.data.content_hash,
            manifest_json=self.data.metadata_json, config=self.config,
            submitted=self._submitted, outcomes=outcomes, events=self.events,
            snapshots=self._history.view(), terminal_state=state,
            terminal_reason=self._generation.terminal_reason,
            lifecycle_stage=self._generation.stage)
        receipt = create_audit_receipt(evidence)
        fills = tuple(o.fill for o in outcomes if o.fill is not None)
        entries, trades = {}, []
        for fill in fills:
            if fill.side == Side.BUY:
                entries[fill.symbol] = fill
            else:
                trades.append(ClosedTrade(fill.symbol, entries.pop(fill.symbol), fill))
        return SimulationResult(self.config, self.data.content_hash, fills=fills,
            outcomes=outcomes, decisions=self._decisions.view(),
            terminal_positions=tuple(p for _, p in sorted(state.positions.items())),
            history=self._history.view(), closed_trades=tuple(trades),
            manifest_json=self.data.metadata_json, audit_receipt=receipt)


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

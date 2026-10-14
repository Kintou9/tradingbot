"""Independent terminal replay for the execution-coupled primary ledger.

This module deliberately does not import or call production mutation,
incremental-validation, marking, sizing, or full-invariant helpers.
"""
from dataclasses import dataclass
import math

from backtesting.primary_state import (
    ExpirationReason, MarkQuality, OrderStatus, Phase, RejectionReason, Side,
)


class TerminalReconciliationError(ValueError):
    pass


@dataclass
class _ReplayPosition:
    entry: object
    intent: object
    cost_basis: float


def reconcile_terminal(*, config, book, submitted, outcomes, events, history,
                       terminal_state, terminal_reason):
    """Rebuild terminal accounting from immutable journal and market evidence."""
    tolerance = 1e-8 * max(1.0, config.initial_cash)

    def fail(label):
        raise TerminalReconciliationError(label)

    def equal(left, right, label):
        if (not math.isfinite(left) or not math.isfinite(right) or
                not math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)):
            fail(label)

    outcomes = tuple(outcomes)
    events = tuple(events)
    history = tuple(history)
    submitted = dict(submitted)
    if terminal_reason not in (ExpirationReason.END_OF_DATASET, ExpirationReason.END_OF_WINDOW):
        fail('invalid terminal expiration context')
    if len(events) != len(history) or any(event.snapshot != snapshot for event, snapshot in zip(events, history)):
        fail('event/snapshot history mismatch')
    if not history or history[-1].at.phase != Phase.CLOSE:
        fail('terminal history must end at close')
    if len(submitted) != len(set(submitted)) or any(key != intent.order_id for key, intent in submitted.items()):
        fail('submitted order identity mismatch')
    if len(outcomes) != len({outcome.order_id for outcome in outcomes}):
        fail('duplicate terminal outcome')
    if set(submitted) != {outcome.order_id for outcome in outcomes}:
        fail('missing or unknown terminal outcome')
    if list(outcomes) != sorted(outcomes, key=lambda outcome: outcome.at):
        fail('outcomes are not chronological')

    submissions = {}
    for intent in submitted.values():
        submissions.setdefault((intent.signal_at.session, intent.signal_at.phase), []).append(intent)
    for values in submissions.values():
        values.sort(key=lambda intent: (intent.symbol, intent.order_id))

    cash = config.initial_cash
    realized_net = realized_gross = fees = slippage = 0.0
    positions = {}
    pending = {}
    outcome_cursor = 0
    last_at = None
    opening_groups = {}

    for event, snapshot in zip(events, history):
        if last_at is not None and snapshot.at <= last_at:
            fail('event history is not strictly chronological')
        last_at = snapshot.at
        if event.kind == 'pending_update':
            if event.order_id is not None:
                fail('pending update has order identity')
            for intent in submissions.get((snapshot.at.session, snapshot.at.phase), ()):
                if intent.symbol in pending:
                    fail('duplicate pending symbol')
                pending[intent.symbol] = intent
        elif event.kind in ('fill', 'rejected', 'expired'):
            if outcome_cursor >= len(outcomes):
                fail('event has no terminal outcome')
            outcome = outcomes[outcome_cursor]
            outcome_cursor += 1
            expected_kind = 'fill' if outcome.status == OrderStatus.FILLED else outcome.status.value
            if event.kind != expected_kind or event.order_id != outcome.order_id or outcome.at != snapshot.at:
                fail('terminal event/outcome mismatch')
            intent = submitted.get(outcome.order_id)
            if intent is None or pending.get(intent.symbol) != intent:
                fail('outcome does not reference active pending order')
            del pending[intent.symbol]
            if outcome.at.phase == Phase.OPEN:
                opening_groups.setdefault(outcome.at, []).append(
                    (intent.side, intent.symbol, intent.order_id))
            if outcome.status == OrderStatus.FILLED:
                fill = outcome.fill
                if fill is None or outcome.reason is not None:
                    fail('filled outcome structure')
                if (fill.order_id != intent.order_id or fill.symbol != intent.symbol or
                        fill.side != intent.side or fill.signal_at != intent.signal_at or
                        fill.at.session != intent.eligible_session or fill.at.phase != Phase.OPEN):
                    fail('fill/intent identity mismatch')
                opening = book.price(fill.symbol, fill.at)
                if opening is None or fill.reference_price != opening:
                    fail('fill reference price mismatch')
                execution = opening * (1 + config.slippage_pct if fill.side == Side.BUY
                                       else 1 - config.slippage_pct)
                fee = fill.quantity * execution * config.fee_pct
                slip = fill.quantity * abs(execution-opening)
                equal(fill.price, execution, 'fill execution price mismatch')
                equal(fill.fee, fee, 'fill fee mismatch')
                equal(fill.slippage_cost, slip, 'fill slippage mismatch')
                if fill.side == Side.BUY:
                    debit = fill.quantity*execution + fee
                    equal(fill.cash_delta, -debit, 'buy cash delta mismatch')
                    if intent.symbol in positions or debit > cash + tolerance:
                        fail('invalid buy capacity or duplicate holding')
                    if debit > cash*config.allocation_pct + tolerance:
                        fail('buy allocation exceeded')
                    cash -= debit
                    positions[intent.symbol] = _ReplayPosition(fill, intent, debit)
                else:
                    held = positions.get(intent.symbol)
                    if (held is None or intent.position_id != held.entry.order_id or
                            fill.quantity != held.entry.quantity):
                        fail('invalid full-position sell')
                    credit = fill.quantity*execution - fee
                    equal(fill.cash_delta, credit, 'sell cash delta mismatch')
                    cash += credit
                    realized_net += credit-held.cost_basis
                    realized_gross += fill.quantity*(opening-held.entry.reference_price)
                    del positions[intent.symbol]
                fees += fee
                slippage += slip
            else:
                if outcome.fill is not None or outcome.cash_delta != 0:
                    fail('nonfill changed cash or contains fill')
                if outcome.status == OrderStatus.REJECTED:
                    if not isinstance(outcome.reason, RejectionReason) or outcome.at.phase != Phase.OPEN:
                        fail('invalid rejection outcome')
                elif not isinstance(outcome.reason, ExpirationReason):
                    fail('invalid expiration reason')
                elif outcome.reason == ExpirationReason.MISSING_EXECUTION_BAR:
                    if outcome.at.phase != Phase.OPEN or book.price(intent.symbol, outcome.at) is not None:
                        fail('invalid missing-bar expiration')
                elif (outcome.reason != terminal_reason or
                      (outcome.at.session, outcome.at.phase) !=
                      (history[-1].at.session, history[-1].at.phase)):
                    fail('incorrect terminal expiration context')
        elif event.kind not in ('opening_marks', 'closing_marks'):
            fail('unknown event kind')
        elif event.order_id is not None:
            fail('mark event has order identity')

        if tuple(sorted(intent.order_id for intent in pending.values())) != event.pending_ids:
            fail('pending journal mismatch')
        marks = dict(snapshot.marks)
        if set(marks) != set(positions):
            fail('snapshot holdings mismatch')
        exposure = 0.0
        for symbol, held in positions.items():
            mark = marks[symbol]
            if mark.value is None or mark.observed_at is None or mark.observed_at > snapshot.at:
                fail('invalid held mark')
            observed = book.price(symbol, mark.observed_at)
            if observed is None or mark.value != observed:
                fail('mark lacks authoritative price')
            current_price = book.price(symbol, snapshot.at)
            expected_quality = MarkQuality.CURRENT if current_price is not None else MarkQuality.STALE
            if mark.quality != expected_quality:
                fail('mark quality mismatch')
            if current_price is not None and (mark.value != current_price or
                    (mark.observed_at.session, mark.observed_at.phase) !=
                    (snapshot.at.session, snapshot.at.phase)):
                fail('current mark mismatch')
            exposure += held.entry.quantity*mark.value
        equal(snapshot.cash, cash, 'snapshot cash mismatch')
        equal(snapshot.exposure, exposure, 'snapshot exposure mismatch')
        equal(snapshot.equity, cash+exposure, 'snapshot equity mismatch')

    if outcome_cursor != len(outcomes) or pending:
        fail('unconsumed outcome or terminal pending order')
    for at, observed_order in opening_groups.items():
        if observed_order != sorted(observed_order, key=lambda item: (item[0] == Side.BUY, item[1], item[2])):
            fail('opening execution order mismatch')
    equal(cash, terminal_state.cash, 'terminal cash mismatch')
    equal(realized_net, terminal_state.realized_net_pnl, 'terminal realized net mismatch')
    equal(realized_gross, terminal_state.realized_gross_pnl, 'terminal realized gross mismatch')
    equal(fees, terminal_state.fees_paid, 'terminal fees mismatch')
    equal(slippage, terminal_state.slippage_costs, 'terminal slippage mismatch')
    if set(positions) != set(terminal_state.positions):
        fail('terminal position symbols mismatch')
    final_marks = dict(history[-1].marks)
    for symbol, held in positions.items():
        actual = terminal_state.positions[symbol]
        if (actual.quantity != held.entry.quantity or actual.entry_order_id != held.entry.order_id or
                actual.reference_entry_price != held.entry.reference_price or
                actual.entry_price != held.entry.price or actual.entry_fee != held.entry.fee or
                actual.cost_basis != held.cost_basis or actual.stop_price != held.intent.stop_price or
                actual.target_price != held.intent.target_price or actual.mark != final_marks[symbol]):
            fail('terminal position mismatch')
    exposure = sum(position.quantity*position.mark.value for position in terminal_state.positions.values())
    equal(exposure, terminal_state.exposure, 'terminal exposure mismatch')
    equal(cash+exposure, terminal_state.equity, 'terminal equity mismatch')
    if terminal_state.pending:
        fail('terminal state retains pending orders')
    return None

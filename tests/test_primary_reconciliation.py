"""Independent terminal-replay corruption tests using offline ledger evidence."""
import copy
from dataclasses import replace

import pytest

from backtesting.primary_ledger import PrimaryLedger
from backtesting.primary_reconciliation import reconcile_terminal, TerminalReconciliationError
from backtesting.primary_state import ExpirationReason, PrimarySimulationConfig, Side
from tests.test_primary_ledger import dataset, order, start, finish


def completed(*, open_position=False):
    data = dataset(4, symbols=('A',), overrides={('A', 2): (120., 120.)})
    config = PrimarySimulationConfig(1000., .5, fee_pct=.01, slippage_pct=.02)
    ledger = PrimaryLedger(data, config)
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    if not open_position:
        ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell-A')])
    result = finish(ledger)
    return ledger, result


def audit(ledger, **changes):
    generation = ledger._generation
    values = dict(config=ledger.config, book=ledger._book, submitted=ledger._submitted,
                  outcomes=ledger.outcomes, events=ledger.events,
                  history=ledger._history.view(), terminal_state=ledger._state,
                  terminal_reason=generation.terminal_reason)
    values.update(changes)
    return reconcile_terminal(**values)


def changed_fill(outcomes, index, field, value):
    result = list(outcomes)
    outcome = copy.copy(result[index])
    fill = copy.copy(outcome.fill)
    object.__setattr__(fill, field, value)
    object.__setattr__(outcome, 'fill', fill)
    result[index] = outcome
    return tuple(result)


@pytest.mark.parametrize('index,field,delta,label', [
    (0, 'reference_price', 1., 'reference'),
    (1, 'reference_price', 1., 'reference'),
    (0, 'price', 1., 'execution'),
    (1, 'fee', 1., 'fee'),
    (0, 'slippage_cost', 1., 'slippage'),
    (1, 'cash_delta', 1., 'cash delta'),
])
def test_terminal_replay_rejects_fill_corruption(index, field, delta, label):
    ledger, _ = completed()
    fill = ledger.outcomes[index].fill
    with pytest.raises(TerminalReconciliationError, match=label):
        audit(ledger, outcomes=changed_fill(ledger.outcomes, index, field,
                                           getattr(fill, field)+delta))


@pytest.mark.parametrize('kind', ['reordered', 'duplicate', 'missing', 'identity'])
def test_terminal_replay_rejects_outcome_corruption(kind):
    ledger, _ = completed()
    outcomes = list(ledger.outcomes)
    if kind == 'reordered':
        outcomes[0], outcomes[1] = outcomes[1], outcomes[0]
    elif kind == 'duplicate':
        outcomes.append(outcomes[-1])
    elif kind == 'missing':
        outcomes.pop()
    else:
        outcome = copy.copy(outcomes[0])
        object.__setattr__(outcome, 'order_id', 'unknown')
        outcomes[0] = outcome
    with pytest.raises(TerminalReconciliationError):
        audit(ledger, outcomes=tuple(outcomes))


@pytest.mark.parametrize('field', ['cash', 'equity', 'realized_net_pnl', 'realized_gross_pnl'])
def test_terminal_replay_rejects_terminal_accounting_corruption(field):
    ledger, _ = completed()
    state = replace(ledger._state)
    setattr(state, field, getattr(state, field)+1)
    with pytest.raises(TerminalReconciliationError, match='terminal'):
        audit(ledger, terminal_state=state)


@pytest.mark.parametrize('field', ['quantity', 'cost_basis'])
def test_terminal_replay_rejects_open_position_corruption(field):
    ledger, _ = completed(open_position=True)
    state = replace(ledger._state, positions=dict(ledger._state.positions))
    position = copy.copy(state.positions['A'])
    object.__setattr__(position, field, getattr(position, field)+1)
    state.positions['A'] = position
    with pytest.raises(TerminalReconciliationError, match='terminal position'):
        audit(ledger, terminal_state=state)


def test_terminal_replay_rejects_snapshot_mark_and_exposure_corruption():
    ledger, _ = completed(open_position=True)
    original_history = ledger._history.view()
    opening_index = next(i for i, event in enumerate(ledger.events)
                         if event.kind == 'opening_marks' and event.snapshot.marks)
    for index in (opening_index, len(original_history)-1):
        history = list(original_history)
        snapshot = history[index]
        symbol, mark = snapshot.marks[0]
        bad_mark = replace(mark, value=mark.value+1)
        history[index] = replace(snapshot, marks=((symbol, bad_mark),))
        events = list(ledger.events)
        events[index] = replace(events[index], snapshot=history[index])
        with pytest.raises(TerminalReconciliationError, match='mark'):
            audit(ledger, history=tuple(history), events=tuple(events))
    history = list(original_history)
    snapshot = history[-1]
    history[-1] = replace(snapshot, exposure=snapshot.exposure+1, equity=snapshot.equity+1)
    events = list(ledger.events)
    events[-1] = replace(events[-1], snapshot=history[-1])
    with pytest.raises(TerminalReconciliationError, match='exposure'):
        audit(ledger, history=tuple(history), events=tuple(events))


def test_terminal_expiration_reason_and_context_are_verified():
    data = dataset(2, symbols=('A',))
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='tail-sell')])
    ledger.finish()
    outcome = copy.copy(ledger.outcomes[-1])
    object.__setattr__(outcome, 'reason', ExpirationReason.END_OF_WINDOW)
    outcomes = ledger.outcomes[:-1] + (outcome,)
    with pytest.raises(TerminalReconciliationError, match='expiration context'):
        audit(ledger, outcomes=outcomes)
    with pytest.raises(TerminalReconciliationError, match='expiration context'):
        audit(ledger, terminal_reason=ExpirationReason.END_OF_WINDOW)


def test_terminal_pending_state_is_rejected():
    ledger, _ = completed()
    state = replace(ledger._state, pending={'A': next(iter(ledger._submitted.values()))})
    with pytest.raises(TerminalReconciliationError, match='pending'):
        audit(ledger, terminal_state=state)


def test_terminal_gate_failure_is_nonmutating_and_retryable(monkeypatch):
    import backtesting.primary_reconciliation as module
    ledger, result = completed()
    generation = ledger._generation
    assert ledger.result() == result
    with monkeypatch.context() as patch:
        patch.setattr(module, 'reconcile_terminal',
                      lambda **kwargs: (_ for _ in ()).throw(TerminalReconciliationError('injected')))
        with pytest.raises(TerminalReconciliationError, match='injected'):
            ledger.result()
    assert ledger._generation is generation
    assert ledger.result() == result


def test_terminal_replay_visits_each_evidence_stream_once():
    ledger, _ = completed()
    counts = {'outcomes': 0, 'events': 0, 'history': 0}
    class Counted:
        def __init__(self, name, values): self.name, self.values = name, values
        def __iter__(self):
            counts[self.name] += 1
            return iter(self.values)
    audit(ledger, outcomes=Counted('outcomes', ledger.outcomes),
          events=Counted('events', ledger.events),
          history=Counted('history', ledger._history.view()))
    assert counts == {'outcomes': 1, 'events': 1, 'history': 1}


def test_every_result_runs_reconciliation_before_receipt(monkeypatch):
    import backtesting.primary_reconciliation as reconciliation
    import backtesting.primary_audit_receipt as receipts
    data = dataset(2, symbols=('A',))
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .5))
    ledger.open_session()
    ledger.close_session()
    ledger.open_session()
    ledger.close_session()
    sequence = []
    original_reconcile = reconciliation.reconcile_terminal
    original_receipt = receipts.create_audit_receipt

    def reconcile(**kwargs):
        sequence.append('reconcile')
        return original_reconcile(**kwargs)

    def receipt(evidence):
        sequence.append('receipt')
        return original_receipt(evidence)

    with monkeypatch.context() as patch:
        patch.setattr(reconciliation, 'reconcile_terminal', reconcile)
        patch.setattr(receipts, 'create_audit_receipt', receipt)
        first = ledger.finish()
        second = ledger.result()
    assert sequence == ['reconcile', 'receipt', 'reconcile', 'receipt']
    assert first == second

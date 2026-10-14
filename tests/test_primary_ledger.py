"""Analytical Step 2 regressions; synthetic intents, never real strategy calls."""
from dataclasses import replace
import math

import pandas as pd
import pytest

from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_ledger import PrimaryLedger, simulate_primary, assert_invariants, size_entry
from backtesting.primary_marking import PriceBook, mark_portfolio
from backtesting.primary_state import (
    EventTime, Phase, PrimarySimulationConfig, OrderIntent, Side, OrderStatus,
    RejectionReason, ExpirationReason, MarkQuality, Fill,
)


def dataset(n=6, symbols=('A', 'B'), missing=None, overrides=None):
    dates = pd.bdate_range('2024-01-02', periods=n)
    frames = {}
    for symbol in symbols:
        frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100., volume=1000.), index=dates)
        for (s, i), values in (overrides or {}).items():
            if symbol == s:
                opening, close = values
                frame.loc[dates[i], ['open', 'high', 'low', 'close']] = [opening, max(opening, close)+1, min(opening, close)*.99, close]
        frames[symbol] = frame.drop([dates[i] for i in (missing or {}).get(symbol, [])])
    return prepare_primary_data(frames, [], session_index=dates,
        metadata=dict(calendar_source='observed_union', price_source='synthetic', adjustment_policy='none'))


def order(ledger, view, symbol='A', side=Side.BUY, order_id='buy-A'):
    index = ledger.data.sessions.index(view.at.session)
    day = ledger.data.sessions[index+1] if index+1 < len(ledger.data.sessions) else None
    return OrderIntent(order_id, symbol, side, view.at, day, 'synthetic', 1., view.equity,
        allocation_pct=ledger.config.allocation_pct if side == Side.BUY else None,
        position_id=view.positions[symbol].entry_order_id if side == Side.SELL else None,
        stop_price=95. if side == Side.BUY else None, target_price=120. if side == Side.BUY else None)


def start(ledger):
    ledger.open_session()
    return ledger.close_session()


def finish(ledger):
    while ledger._index < len(ledger.data.sessions)-1:
        ledger.open_session()
        ledger.close_session()
    return ledger.finish()


def leakage_run(exit_price=120.):
    data = dataset(overrides={('A', 4): (exit_price, exit_price)})
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., 1.))
    view = start(ledger)
    ledger.submit_close([order(ledger, view, 'B', order_id='rejected-B'), order(ledger, view)])
    opening = ledger.open_session()
    assert opening.cash == 0 and set(opening.positions) == {'A'}
    assert opening.positions['A'].quantity == 10
    assert ledger.state.outcomes[1].status == OrderStatus.REJECTED
    assert ledger.state.outcomes[1].reason == RejectionReason.INSUFFICIENT_CASH
    ledger.close_session()
    for _ in range(2):
        ledger.open_session()
        view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='exit-A')])
    result = finish(ledger)
    return ledger, result


def test_future_cash_cannot_finance_an_earlier_rejected_order():
    ledger, result = leakage_run()
    assert [f.order_id for f in result.fills] == ['buy-A', 'exit-A']
    assert ledger.state.cash == 1200.
    assert ledger.state.realized_net_pnl == 200.
    assert not ledger.state.positions
    assert all(p.symbol != 'B' for event in ledger.events for p in event.positions)
    assert sum(o.order_id == 'rejected-B' for o in result.outcomes) == 1
    changed, _ = leakage_run(200.)
    cutoff = ledger.data.sessions[3]
    assert [e for e in ledger.events if e.snapshot.at.session <= cutoff] == [e for e in changed.events if e.snapshot.at.session <= cutoff]


def test_sell_before_buy_funds_only_a_new_order():
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., 1.))
    view = start(ledger)
    ledger.submit_close([order(ledger, view), order(ledger, view, 'B', order_id='old-B')])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, 'B', order_id='new-B'), order(ledger, view, side=Side.SELL, order_id='exit-A')])
    opening = ledger.open_session()
    assert list(opening.positions) == ['B']
    assert opening.cash == 0.
    fills = [o.fill.order_id for o in ledger.state.outcomes if o.fill]
    assert fills == ['buy-A', 'exit-A', 'new-B']
    assert ledger.state.outcomes[1].order_id == 'old-B'
    assert ledger.state.outcomes[1].status == OrderStatus.REJECTED
    assert opening.positions['B'].quantity == 10


def test_cost_inclusive_sizing_and_round_trip_pnl():
    data = dataset(overrides={('A', 2): (120., 120.)})
    config = PrimarySimulationConfig(1000., .5, fee_pct=.01, slippage_pct=.01)
    ledger = PrimaryLedger(data, config)
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    opening = ledger.open_session()
    quantity = 500/(101*1.01)
    buy = ledger.state.outcomes[0].fill
    assert buy.price == 101.
    assert buy.quantity == pytest.approx(quantity)
    assert buy.fee == pytest.approx(quantity*101*.01)
    assert buy.cash_delta == pytest.approx(-500)
    assert opening.cash == pytest.approx(500.)
    assert opening.positions['A'].cost_basis == pytest.approx(500.)
    assert opening.equity == pytest.approx(500+quantity*100)
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell')])
    opening = ledger.open_session()
    sell = ledger.state.outcomes[-1].fill
    credit = quantity*118.8*.99
    assert sell.price == pytest.approx(118.8)
    assert sell.cash_delta == pytest.approx(credit)
    assert opening.cash == pytest.approx(500+credit)
    assert ledger.state.realized_net_pnl == pytest.approx(credit-500)
    gross = quantity*20
    assert ledger.state.realized_gross_pnl == pytest.approx(gross)
    assert ledger.state.realized_net_pnl == pytest.approx(gross-ledger.state.fees_paid-ledger.state.slippage_costs)
    assert not opening.positions


def test_missing_next_execution_bar_expires_without_rolling_forward():
    ledger = PrimaryLedger(dataset(missing={'A': [1]}), PrimarySimulationConfig(1000., 1.))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    result = finish(ledger)
    assert not result.fills and not result.terminal_positions
    outcome = result.outcomes[0]
    assert outcome.status == OrderStatus.EXPIRED
    assert outcome.reason == ExpirationReason.MISSING_EXECUTION_BAR
    assert outcome.at.session == ledger.data.sessions[1]
    assert all(s.cash == s.equity == 1000 for s in result.history)


def test_open_close_isolation_and_no_intraday_stop():
    data = dataset(overrides={('A', 1): (100., 20.)})
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    opening = ledger.open_session()
    assert opening.cash == 500 and opening.equity == 1000
    assert opening.positions['A'].quantity == 5
    closing = ledger.close_session()
    assert closing.equity == 600 and closing.cash == 500
    assert closing.positions['A'].stop_price == 95
    # Low/close passed the stop: no autonomous strategy exits in Step 2.
    result = finish(ledger)
    assert len(result.fills) == 1 and len(result.terminal_positions) == 1


def test_stale_mark_blocks_buy_not_just_a_warning():
    ledger = PrimaryLedger(dataset(missing={'A': [2]}), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, 'B', order_id='blocked')])
    opening = ledger.open_session()
    assert opening.positions['A'].mark.quality == MarkQuality.STALE
    assert opening.positions['A'].mark.observed_at == view.positions['A'].mark.observed_at
    assert ledger.state.outcomes[-1].reason == RejectionReason.STALE_PORTFOLIO_MARKS
    assert opening.cash == 500 and set(opening.positions) == {'A'}


def test_missing_sell_keeps_position_and_can_be_reissued():
    ledger = PrimaryLedger(dataset(missing={'A': [2]}), PrimarySimulationConfig(1000., 1.))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='missing-sell')])
    ledger.open_session()
    ledger.close_session()
    ledger.open_session()
    view = ledger.close_session()
    assert view.positions['A'].quantity == 10
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='new-sell')])
    result = finish(ledger)
    assert [o.status for o in result.outcomes] == [OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.FILLED]
    assert not result.terminal_positions and ledger.state.cash == 1000.


def test_alphabetical_input_order_is_deterministic():
    def run(reverse):
        data = dataset(symbols=('B', 'A') if reverse else ('A', 'B'))
        ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .6))
        view = start(ledger)
        intents = [order(ledger, view, 'A'), order(ledger, view, 'B', order_id='buy-B')]
        ledger.submit_close(intents[::-1] if reverse else intents)
        result = finish(ledger)
        assert ledger.state.cash == 160.
        assert [f.quantity for f in result.fills] == [6., 2.4]
        return ledger.events, result
    assert run(False) == run(True)


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
def test_tail_intent_expires_at_final_close(side):
    ledger = PrimaryLedger(dataset(2), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    if side == Side.SELL:
        ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    before = ledger.state
    ledger.submit_close([order(ledger, view, side=side, order_id='tail')])
    result = ledger.finish()
    assert result.outcomes[-1].reason == ExpirationReason.END_OF_DATASET
    assert result.outcomes[-1].cash_delta == 0
    assert ledger.state.cash == before.cash
    assert ledger.state.realized_net_pnl == before.realized_net_pnl
    assert {p.symbol: p.quantity for p in result.terminal_positions} == {s:p.quantity for s,p in before.positions.items()}


@pytest.mark.parametrize('kind', ['duplicate_id', 'duplicate_symbol', 'orphan_sell', 'wrong_day', 'wrong_equity', 'wrong_allocation'])
def test_invalid_submission_is_atomic(kind):
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    first = order(ledger, view)
    batch = [first]
    if kind == 'duplicate_id':
        batch.append(order(ledger, view, 'B'))
    elif kind == 'duplicate_symbol':
        batch.append(replace(first, order_id='different'))
    elif kind == 'orphan_sell':
        batch = [replace(first, side=Side.SELL, position_id='absent', allocation_pct=None, stop_price=None, target_price=None)]
    elif kind == 'wrong_day':
        batch = [replace(first, eligible_session=ledger.data.sessions[3])]
    elif kind == 'wrong_equity':
        batch = [replace(first, decision_equity=2000.)]
    else:
        batch = [replace(first, allocation_pct=.1)]
    before, events = ledger.state, ledger.events
    with pytest.raises(ValueError):
        ledger.submit_close(batch)
    assert ledger.state == before and ledger.events == events and not ledger.submitted


def test_reusing_terminal_id_does_not_corrupt_state():
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., 1.))
    view = start(ledger)
    ledger.submit_close([order(ledger, view), order(ledger, view, 'B', order_id='old')])
    ledger.open_session()
    view = ledger.close_session()
    before = ledger.state
    with pytest.raises(ValueError, match='Duplicate'):
        ledger.submit_close([order(ledger, view, 'B', order_id='old')])
    assert ledger.state == before


def test_partial_or_changed_fill_fails_atomically(monkeypatch):
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell')])
    original = ledger.apply_fill
    def attempt_partial(fill):
        before = ledger.state
        partial = replace(fill, quantity=fill.quantity/2, cash_delta=fill.cash_delta/2,
                          fee=fill.fee/2, slippage_cost=fill.slippage_cost/2)
        with pytest.raises(ValueError, match='partial'):
            original(partial)
        assert ledger.state == before
        original(fill)
    monkeypatch.setattr(ledger, 'apply_fill', attempt_partial)
    ledger.open_session()
    assert ledger.state.cash == 1000 and not ledger.state.positions


def _tail_pending_ledger():
    ledger = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='tail-sell')])
    return ledger


def _publication_observation(ledger):
    state = ledger.state
    return (state, ledger.outcomes, ledger.events, dict(ledger.submitted))


def _pending_fill_ledger(monkeypatch, config=None):
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), config or PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    captured = []
    with monkeypatch.context() as patch:
        patch.setattr(ledger, 'apply_fill',
                      lambda fill: (captured.append(fill), (_ for _ in ()).throw(RuntimeError('capture')))[1])
        with pytest.raises(RuntimeError, match='capture'):
            ledger.open_session()
    assert ledger._stage == 'open' and len(captured) == 1
    return ledger, captured[0]


def test_ledger_owned_journal_is_immutable_ordered_and_isolated():
    left = _tail_pending_ledger()
    before = left.outcomes
    assert isinstance(before, tuple) and [o.order_id for o in before] == ['buy-A']
    right = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    assert right.outcomes == () and right.outcomes is not left.outcomes
    result = left.finish()
    assert [o.order_id for o in result.outcomes] == ['buy-A', 'tail-sell']
    assert before == (result.outcomes[0],)
    assert left.state.outcomes == result.outcomes == left.outcomes
    with pytest.raises((AttributeError, TypeError)):
        left.outcomes.append(result.outcomes[-1])
    assert right.outcomes == ()


@pytest.mark.parametrize('failure_point', [
    'outcome_construction', 'journal_preparation', 'candidate_validation',
    'snapshot_construction', 'before_publication',
])
def test_terminal_publication_failure_is_atomic_and_recoverable(monkeypatch, failure_point):
    import backtesting.primary_ledger as module
    ledger = _tail_pending_ledger()
    before = _publication_observation(ledger)
    message = 'injected-' + failure_point

    with monkeypatch.context() as patch:
        if failure_point == 'outcome_construction':
            patch.setattr(module, 'OrderOutcome', lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'journal_preparation':
            patch.setattr(module._OutcomeJournal, 'appended',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'candidate_validation':
            patch.setattr(module, 'validate_transition',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'snapshot_construction':
            patch.setattr(module, 'PortfolioSnapshot',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        else:
            patch.setattr(ledger, '_before_publication',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        with pytest.raises(RuntimeError, match=message):
            ledger.finish()

    assert _publication_observation(ledger) == before
    result = ledger.finish()
    assert [o.order_id for o in result.outcomes] == ['buy-A', 'tail-sell']
    assert result.outcomes[-1].reason == ExpirationReason.END_OF_DATASET
    assert result.terminal_positions


def test_stage1_journal_matches_independent_round_trip_arithmetic():
    data = dataset(n=4, symbols=('A',), overrides={('A', 2): (120., 120.)})
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .5, fee_pct=.01, slippage_pct=.01))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell-A')])
    result = finish(ledger)
    buy, sell = result.fills
    expected_cash = 1000. + buy.cash_delta + sell.cash_delta
    assert ledger.state.cash == pytest.approx(expected_cash)
    assert ledger.state.realized_net_pnl == pytest.approx(sell.cash_delta + buy.cash_delta)
    assert [o.order_id for o in result.outcomes] == ['buy-A', 'sell-A']
    assert not result.terminal_positions


@pytest.mark.parametrize('failure_point', [
    'outcome_construction', 'journal_preparation', 'candidate_validation',
    'snapshot_construction', 'generation_construction', 'before_publication',
])
def test_accepted_fill_publication_failure_preserves_generation(monkeypatch, failure_point):
    import backtesting.primary_ledger as module
    ledger, fill = _pending_fill_ledger(monkeypatch)
    generation = ledger._generation
    before = _publication_observation(ledger)
    message = 'accepted-' + failure_point
    with monkeypatch.context() as patch:
        if failure_point == 'outcome_construction':
            patch.setattr(module, 'OrderOutcome',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'journal_preparation':
            patch.setattr(module._OutcomeJournal, 'appended',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'candidate_validation':
            patch.setattr(module, 'validate_transition',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'snapshot_construction':
            patch.setattr(module, 'PortfolioSnapshot',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        elif failure_point == 'generation_construction':
            patch.setattr(module, '_LedgerGeneration',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        else:
            patch.setattr(ledger, '_before_publication',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        with pytest.raises(RuntimeError, match=message):
            ledger.apply_fill(fill)
    assert ledger._generation is generation
    assert ledger._stage == 'open'
    assert _publication_observation(ledger) == before
    ledger.apply_fill(fill)
    assert ledger.state.cash == 500 and set(ledger.state.positions) == {'A'}
    assert [o.order_id for o in ledger.outcomes] == ['buy-A']


@pytest.mark.parametrize('failure_point', ['event_append', 'snapshot_append', 'decision_append'])
def test_history_preparation_failure_does_not_publish(monkeypatch, failure_point):
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    ledger.open_session()
    view = ledger.close_session()
    generation = ledger._generation
    before = _publication_observation(ledger)
    message = 'history-' + failure_point
    with monkeypatch.context() as patch:
        if failure_point == 'decision_append':
            patch.setattr(module._PersistentHistory, 'extended',
                          lambda *a, **k: (_ for _ in ()).throw(RuntimeError(message)))
        else:
            original = module._PersistentHistory.appended
            wanted = module.LedgerEvent if failure_point == 'event_append' else module.PortfolioSnapshot
            def injected(self, value):
                if isinstance(value, wanted):
                    raise RuntimeError(message)
                return original(self, value)
            patch.setattr(module._PersistentHistory, 'appended', injected)
        with pytest.raises(RuntimeError, match=message):
            ledger.submit_close([order(ledger, view)])
    assert ledger._generation is generation
    assert _publication_observation(ledger) == before
    assert ledger._stage == 'close' and not ledger._close_submitted
    ledger.submit_close([order(ledger, view)])
    assert ledger._close_submitted


def test_successful_publication_swaps_one_generation_and_views_remain_stable(monkeypatch):
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    roots = []
    prepared = []
    original_hook = ledger._before_publication
    def observe(next_generation):
        roots.append(ledger._generation)
        prepared.append(next_generation)
        original_hook(next_generation)
    monkeypatch.setattr(ledger, '_before_publication', observe)
    old_events = ledger.events
    old_history = ledger._history.view()
    old_decisions = ledger._decisions.view()
    ledger.open_session()
    assert len(roots) == 1 and ledger._generation is prepared[0]
    assert roots[0] is not ledger._generation
    assert old_events == () and old_history == () and old_decisions == ()
    event_view = ledger.events
    snapshot_view = ledger._history.view()
    ledger.close_session()
    view = ledger.state.decision_view()
    ledger.submit_close([order(ledger, view)])
    assert event_view == (ledger.events[0],)
    assert snapshot_view == (ledger._history.view()[0],)
    assert old_decisions == () and len(ledger._decisions.view()) == 1


def test_persistent_histories_are_isolated_between_ledgers():
    left = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    right = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    left.open_session()
    assert len(left.events) == 1 and right.events == ()
    assert left._history.view() and right._history.view() == ()
    assert left._decisions.view() == right._decisions.view() == ()


def test_publication_does_not_materialize_accumulated_histories(monkeypatch):
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    with monkeypatch.context() as patch:
        patch.setattr(module._PersistentHistory, 'view',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('hot-path materialization')))
        ledger.open_session()
        ledger.close_session()
        ledger.submit_close(())
    assert len(ledger.events) == 3


def test_normal_publication_uses_incremental_validation_without_full_replay(monkeypatch):
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    calls = []
    original = module.validate_transition

    def counted(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, 'validate_transition', counted)
        patch.setattr(module, 'assert_invariants',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('full replay reached')))
        view = start(ledger)
        ledger.submit_close([order(ledger, view)])
        ledger.open_session()
        ledger.close_session()
        ledger.submit_close(())
        ledger.open_session()
        ledger.close_session()
        result = ledger.finish()
    assert calls == [event.kind for event in ledger.events]
    assert result.audit_receipt is not None


def test_publication_does_not_materialize_outcome_journal(monkeypatch):
    import backtesting.primary_ledger as module
    ledger, fill = _pending_fill_ledger(monkeypatch)
    previous = ledger._journal
    with monkeypatch.context() as patch:
        patch.setattr(module._OutcomeJournal, 'view',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('journal scan')))
        ledger.apply_fill(fill)
    assert ledger._journal.size == 1
    assert ledger._journal.head.previous is previous.head
    assert ledger._state.outcomes == ()


def _captured_buy_candidate(monkeypatch, config=None):
    ledger, fill = _pending_fill_ledger(monkeypatch, config)
    captured = []
    with monkeypatch.context() as patch:
        patch.setattr(ledger, '_publish', lambda *a, **k: (captured.append((a, k)),
                      (_ for _ in ()).throw(RuntimeError('captured')))[1])
        with pytest.raises(RuntimeError, match='captured'):
            ledger.apply_fill(fill)
    args, kwargs = captured[0]
    return ledger, args[0], kwargs['outcome']


def _captured_sell_candidate(monkeypatch, config):
    ledger = PrimaryLedger(dataset(4, symbols=('A',), overrides={('A', 2): (120., 120.)}), config)
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell-A')])
    fills = []
    with monkeypatch.context() as patch:
        patch.setattr(ledger, 'apply_fill',
                      lambda fill: (fills.append(fill), (_ for _ in ()).throw(RuntimeError('capture-sell')))[1])
        with pytest.raises(RuntimeError, match='capture-sell'):
            ledger.open_session()
    captured = []
    with monkeypatch.context() as patch:
        patch.setattr(ledger, '_publish', lambda *a, **k: (captured.append((a, k)),
                      (_ for _ in ()).throw(RuntimeError('captured')))[1])
        with pytest.raises(RuntimeError, match='captured'):
            ledger.apply_fill(fills[0])
    args, kwargs = captured[0]
    return ledger, args[0], kwargs['outcome'], fills[0]


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
def test_incremental_fill_reference_price_with_nonzero_slippage(monkeypatch, side):
    import copy
    import backtesting.primary_ledger as module
    config = PrimarySimulationConfig(1000., .5, fee_pct=.01, slippage_pct=.02)
    if side == Side.BUY:
        ledger, candidate, outcome = _captured_buy_candidate(monkeypatch, config)
        valid_fill = outcome.fill
    else:
        ledger, candidate, outcome, valid_fill = _captured_sell_candidate(monkeypatch, config)
    opening = ledger._book.price(valid_fill.symbol, valid_fill.at)
    assert valid_fill.reference_price == opening
    assert valid_fill.price == pytest.approx(opening * (1.02 if side == Side.BUY else .98))

    generation = ledger._generation
    corrupted_fill = copy.copy(valid_fill)
    object.__setattr__(corrupted_fill, 'reference_price', opening + 1)
    corrupted_outcome = copy.copy(outcome)
    object.__setattr__(corrupted_outcome, 'fill', corrupted_fill)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'assert_invariants',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('full replay reached')))
        with pytest.raises(ValueError, match='Incremental invariant: fill reference price'):
            ledger._publish(candidate, 'fill', valid_fill.order_id, outcome=corrupted_outcome)
    assert ledger._generation is generation

    ledger.apply_fill(valid_fill)
    assert ledger.outcomes[-1].fill.reference_price == opening
    assert ledger.outcomes[-1].fill.side == side


def test_incremental_fill_identity_and_buy_allocation_are_checked_locally(monkeypatch):
    import copy
    import backtesting.primary_ledger as module
    ledger, candidate, outcome = _captured_buy_candidate(monkeypatch)
    generation = ledger._generation

    for field, value in [('side', Side.SELL),
                         ('signal_at', replace(outcome.fill.signal_at, sequence=1))]:
        corrupted_fill = copy.copy(outcome.fill)
        object.__setattr__(corrupted_fill, field, value)
        corrupted = copy.copy(outcome)
        object.__setattr__(corrupted, 'fill', corrupted_fill)
        with pytest.raises(ValueError, match='fill identity'):
            module.validate_transition(generation, candidate, 'fill', generation.submitted,
                ledger._book, ledger.config, outcome=corrupted)

    with pytest.raises(ValueError, match='buy allocation exceeded'):
        module.validate_transition(generation, candidate, 'fill', generation.submitted,
            ledger._book, replace(ledger.config, allocation_pct=.4), outcome=outcome)
    assert ledger._generation is generation


@pytest.mark.parametrize('corruption', ['cash', 'quantity', 'cost_basis', 'realized',
                                         'missing_outcome', 'wrong_status'])
def test_incremental_buy_validator_rejects_corruption_before_full_replay(monkeypatch, corruption):
    import backtesting.primary_ledger as module
    ledger, candidate, outcome = _captured_buy_candidate(monkeypatch)
    generation = ledger._generation
    if corruption == 'cash':
        candidate.cash += 1
        candidate.equity += 1
    elif corruption in ('quantity', 'cost_basis'):
        position = candidate.positions['A']
        object.__setattr__(position, corruption, getattr(position, corruption) + 1)
    elif corruption == 'realized':
        candidate.realized_net_pnl += 1
    elif corruption == 'missing_outcome':
        outcome = None
    else:
        object.__setattr__(outcome, 'status', OrderStatus.REJECTED)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'assert_invariants',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('full replay reached')))
        with pytest.raises(ValueError, match='Incremental invariant'):
            ledger._publish(candidate, 'fill', 'buy-A', outcome=outcome)
    assert ledger._generation is generation


def test_incremental_nonfill_reasons_and_chronology_are_independent(monkeypatch):
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(3, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, side=Side.SELL, order_id='sell')])
    ledger.open_session()
    view = ledger.close_session()
    ledger.submit_close([order(ledger, view, order_id='tail-buy')])
    current = ledger._generation
    intent = current.state.pending['A']
    candidate = module._advance(current.state, replace(current.state.at, sequence=current.state.at.sequence+1))
    del candidate.pending['A']

    wrong_context = module.OrderOutcome(intent.order_id, OrderStatus.EXPIRED, candidate.at,
                                        ExpirationReason.MISSING_EXECUTION_BAR)
    with pytest.raises(ValueError, match='missing-bar'):
        module.validate_transition(current, candidate, 'expired', current.submitted,
            ledger._book, ledger.config, outcome=wrong_context)
    terminal = module.OrderOutcome(intent.order_id, OrderStatus.EXPIRED, candidate.at,
                                   ExpirationReason.END_OF_DATASET)
    bad_meta = replace(current.validation, last_outcome_at=replace(candidate.at, sequence=candidate.at.sequence+1))
    with pytest.raises(ValueError, match='chronology'):
        module.validate_transition(replace(current, validation=bad_meta), candidate, 'expired',
            current.submitted, ledger._book, ledger.config, outcome=terminal)
    object.__setattr__(terminal, 'reason', RejectionReason.INSUFFICIENT_CASH)
    with pytest.raises(ValueError, match='expiration'):
        module.validate_transition(current, candidate, 'expired', current.submitted,
            ledger._book, ledger.config, outcome=terminal)
    assert ledger._generation is current


@pytest.mark.parametrize('field,value', [('outcome_count', 1), ('fill_count', 1),
                                         ('cumulative_cash_delta', 1.0)])
def test_incremental_metadata_corruption_is_detected_without_history_replay(monkeypatch, field, value):
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    current = ledger._generation
    candidate = mark_portfolio(current.state, ledger._book,
                               EventTime(ledger.data.sessions[0], Phase.OPEN))
    corrupted = replace(current, validation=replace(current.validation, **{field: value}))
    with monkeypatch.context() as patch:
        patch.setattr(module, 'assert_invariants',
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError('full replay reached')))
        with pytest.raises(ValueError, match='metadata'):
            module.validate_transition(corrupted, candidate, 'opening_marks', current.submitted,
                ledger._book, ledger.config, outcome=None, index=0, stage='open')
    assert ledger._generation is current


def test_incremental_rejection_reason_and_stale_mark_corruption(monkeypatch):
    import backtesting.primary_ledger as module
    ledger, _ = _pending_fill_ledger(monkeypatch)
    current = ledger._generation
    intent = current.state.pending['A']
    candidate = module._advance(current.state, replace(current.state.at, sequence=current.state.at.sequence+1))
    del candidate.pending['A']
    rejected = module.OrderOutcome(intent.order_id, OrderStatus.REJECTED, candidate.at,
                                   RejectionReason.INSUFFICIENT_CASH)
    object.__setattr__(rejected, 'reason', ExpirationReason.END_OF_DATASET)
    with pytest.raises(ValueError, match='rejection'):
        module.validate_transition(current, candidate, 'rejected', current.submitted,
            ledger._book, ledger.config, outcome=rejected)

    data = dataset(4, symbols=('A',), missing={'A': [2]})
    marked = PrimaryLedger(data, PrimarySimulationConfig(1000., .5))
    view = start(marked)
    marked.submit_close([order(marked, view)])
    marked.open_session()
    marked.close_session()
    prior = marked._generation
    at = EventTime(data.sessions[2], Phase.OPEN)
    stale = mark_portfolio(prior.state, marked._book, at)
    position = stale.positions['A']
    stale.positions['A'] = replace(position, mark=replace(position.mark, value=position.mark.value+1))
    with pytest.raises(ValueError, match='stale mark'):
        module.validate_transition(prior, stale, 'opening_marks', prior.submitted,
            marked._book, marked.config, outcome=None, index=2, stage='open')
    assert marked._generation is prior


def test_incremental_validator_does_not_visit_historical_outcomes():
    import backtesting.primary_ledger as module
    ledger = PrimaryLedger(dataset(2, symbols=('A',)), PrimarySimulationConfig(1000., .5))
    ledger.open_session()
    current = ledger._generation
    at = EventTime(ledger.data.sessions[0], Phase.CLOSE)
    candidate = mark_portfolio(current.state, ledger._book, at)
    class PoisonHistory:
        def __iter__(self):
            raise AssertionError('historical outcome visited')
        def __len__(self):
            raise AssertionError('historical outcome counted')
    poisoned_state = replace(current.state, outcomes=PoisonHistory())
    metadata = module.validate_transition(replace(current, state=poisoned_state), candidate,
        'closing_marks', current.submitted, ledger._book, ledger.config,
        outcome=None, stage='close', close_submitted=False)
    assert metadata is current.validation


def test_journal_invariants_detect_balanced_but_fake_ledger():
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., .5))
    start(ledger)
    fake = ledger.state
    fake.cash += 10
    fake.equity += 10
    fake.realized_net_pnl += 10
    fake.validate()  # Snapshot-only equations cannot detect fabricated proceeds.
    with pytest.raises(ValueError, match='cash movements'):
        assert_invariants(fake, ledger.submitted, ledger._book, ledger.config)


def test_zero_orders_and_readonly_boundary():
    ledger = PrimaryLedger(dataset(3), PrimarySimulationConfig(1000., .1))
    view = start(ledger)
    with pytest.raises(TypeError):
        view.positions['A'] = None
    external = ledger.state
    external.cash = 0
    assert ledger.state.cash == 1000.
    result = finish(ledger)
    assert not result.fills and not result.outcomes
    assert all(s.cash == s.equity == 1000. for s in result.history)
    assert len([e for e in ledger.events if e.kind == 'closing_marks']) == 3


@pytest.mark.parametrize('increment', [None, .0001, 1., 100.])
def test_quantity_granularity_and_no_overspending(increment):
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., 1., fee_pct=.013, slippage_pct=.007, quantity_increment=increment))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    assert ledger.state.cash >= 0
    if increment == 100.:
        assert ledger.state.outcomes[0].reason == RejectionReason.QUANTITY_ROUNDS_TO_ZERO
    else:
        fill = ledger.state.outcomes[0].fill
        assert -fill.cash_delta <= 1000.
        if increment:
            assert fill.quantity/increment == pytest.approx(round(fill.quantity/increment))


def test_minimum_notional_rejection_does_not_spend_cash():
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    assert ledger.state.outcomes[0].reason == RejectionReason.BELOW_MIN_NOTIONAL
    assert ledger.state.cash == 1.


def test_offline_fixed_stream_replay_and_phase_guards():
    data, config = dataset(3), PrimarySimulationConfig(1000., .5)
    intent = OrderIntent('fixed', 'A', Side.BUY, EventTime(data.sessions[0], Phase.CLOSE),
                        data.sessions[1], 'synthetic', 1., 1000., .5, stop_price=95., target_price=120.)
    result = simulate_primary(data, config, [intent])
    assert result.fills[0].quantity == 5
    ledger = PrimaryLedger(data, config)
    with pytest.raises(ValueError):
        ledger.close_session()
    with pytest.raises(ValueError):
        ledger.finish()
    ledger.open_session()
    with pytest.raises(ValueError):
        ledger.open_session()


def test_policy_mismatch_rejected_before_execution():
    with pytest.raises(ValueError, match='policies'):
        PrimaryLedger(dataset(), PrimarySimulationConfig(1000., .5, valuation_mode='legacy_research_inputs'))


def test_journal_detects_rewritten_entry_levels_and_cost_totals():
    ledger = PrimaryLedger(dataset(), PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    fake = ledger.state
    fake.positions['A'] = replace(fake.positions['A'], stop_price=80.)
    with pytest.raises(ValueError, match='metadata'):
        assert_invariants(fake, ledger.submitted, ledger._book, ledger.config)
    fake = ledger.state
    fake.fees_paid = 1.
    with pytest.raises(ValueError, match='fee totals'):
        assert_invariants(fake, ledger.submitted, ledger._book, ledger.config)


@pytest.mark.parametrize('cash,fee,slip,opening', [
    (1000., .003, .007, 137.31), (1234.56, .0013, .0027, .37),
    (10., .9, .9, 1.13), (100000., .00001, .00003, 1001.01),
])
def test_rounding_never_hides_cash_overdraft(cash, fee, slip, opening):
    data = dataset(overrides={('A', 1): (opening, opening)})
    ledger = PrimaryLedger(data, PrimarySimulationConfig(cash, 1., fee_pct=fee, slippage_pct=slip))
    view = start(ledger)
    ledger.submit_close([order(ledger, view)])
    ledger.open_session()
    fill = ledger.state.outcomes[0].fill
    assert fill is not None
    assert -fill.cash_delta <= cash
    assert ledger.state.cash == cash+fill.cash_delta >= 0.
    assert ledger.state.equity == pytest.approx(ledger.state.cash+fill.quantity*opening)


def test_driver_rejects_same_session_fills_and_outside_signals():
    data = dataset()
    with pytest.raises(ValueError):
        OrderIntent('bad', 'A', Side.BUY, EventTime(data.sessions[0], Phase.CLOSE), data.sessions[0],
                    'synthetic', 1., 1000., .5, stop_price=95., target_price=120.)
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000., .5))
    view = start(ledger)
    fixed = order(ledger, view)
    from datetime import date
    outside = replace(fixed, signal_at=EventTime(date(2020, 1, 1), Phase.CLOSE))
    with pytest.raises(ValueError, match='outside'):
        simulate_primary(data, ledger.config, [outside])


def test_fresh_process_ledger_is_offline_and_ignores_environment():
    import subprocess
    import sys
    code = '''
import socket, os, sys
socket.socket.connect = lambda *a: (_ for _ in ()).throw(AssertionError('network forbidden'))
os.environ['MAX_POSITION_PCT']='invalid'
os.environ['BACKTEST_FEES_PCT']='invalid'
from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_state import PrimarySimulationConfig
from backtesting.primary_ledger import simulate_primary
import pandas as pd
idx=pd.date_range('2024-01-02',periods=3)
f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=1.),index=idx)
d=prepare_primary_data({'A':f},[],session_index=idx,metadata=dict(calendar_source='observed_union',price_source='synthetic',adjustment_policy='none'))
r=simulate_primary(d,PrimarySimulationConfig(1000.,.5))
assert r.history[-1].equity==1000.
assert not any(m.startswith(('broker.', 'db.', 'anthropic', 'engine.valuation_engine')) for m in sys.modules)
'''
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

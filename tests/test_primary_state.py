"""Step 1 contract tests; no simulated execution or services."""
from dataclasses import replace
from datetime import date

import pytest

from backtesting.primary_state import (
    PrimarySimulationConfig, EventTime, Phase, Mark, MarkQuality, Position,
    PortfolioState, Side, OrderIntent, OrderStatus, OrderOutcome, ExpirationReason,
    Fill, DecisionRecord, DecisionAction,
)

D0, D1 = date(2024, 1, 2), date(2024, 1, 3)
SIGNAL, OPEN, CLOSE = EventTime(D0, Phase.CLOSE), EventTime(D1, Phase.OPEN), EventTime(D1, Phase.CLOSE)


def position():
    return Position('A', 1., 'entry-1', SIGNAL, OPEN, 100., 100., 0., 100., 95., 120.,
                    Mark(100., CLOSE, CLOSE, MarkQuality.CURRENT))


def intent(**kw):
    args = dict(order_id='buy-1', symbol='B', side=Side.BUY, signal_at=CLOSE,
                eligible_session=date(2024, 1, 4), reason='valuation_trigger', confidence=.8,
                decision_equity=1000., allocation_pct=.1, stop_price=95., target_price=120.)
    return OrderIntent(**(args | kw))


def test_valid_config_is_frozen_and_does_not_read_env(monkeypatch):
    monkeypatch.setenv('MAX_POSITION_PCT', 'garbage')
    cfg = PrimarySimulationConfig(1000., .1)
    assert cfg.valuation_mode.value == 'verified_filing_inputs'
    assert 'live_exposure_cap' in cfg.unmodeled_live_controls
    with pytest.raises(AttributeError):
        cfg.initial_cash = 20


@pytest.mark.parametrize('field,value', [
    ('initial_cash', 0), ('initial_cash', -1), ('initial_cash', float('nan')),
    ('initial_cash', float('inf')), ('initial_cash', True), ('initial_cash', '100'),
    ('allocation_pct', 0), ('allocation_pct', -1), ('allocation_pct', 1.1),
    ('allocation_pct', float('nan')), ('fee_pct', -1), ('fee_pct', 1),
    ('slippage_pct', float('inf')), ('slippage_pct', 1), ('slippage_pct', -.1),
    ('quantity_increment', 0), ('quantity_increment', -1), ('quantity_increment', float('nan')),
    ('min_trade_notional', 0), ('min_trade_notional', 2), ('valuation_mode', 'auto'),
    ('calendar_policy', 'weekdays'), ('ordering_policy', 'random'), ('schema_version', 'unknown'),
])
def test_invalid_config(field, value):
    with pytest.raises(ValueError):
        PrimarySimulationConfig(**(dict(initial_cash=1000., allocation_pct=.1) | {field: value}))


def test_stale_marks_distinguish_previous_close_from_current_open():
    stale = Mark(100., SIGNAL, OPEN, MarkQuality.STALE)
    assert stale.observed_at.phase == Phase.CLOSE
    assert stale.as_of.phase == Phase.OPEN
    missing = Mark(None, None, OPEN, MarkQuality.MISSING)
    assert missing.value is None
    with pytest.raises(ValueError):
        Mark(110., CLOSE, OPEN, MarkQuality.STALE)
    with pytest.raises(ValueError):
        Mark(100., SIGNAL, OPEN, MarkQuality.CURRENT)
    with pytest.raises(ValueError):
        Mark(100., None, OPEN, MarkQuality.MISSING)


@pytest.mark.parametrize('kw', [dict(quantity=0), dict(quantity=float('inf')),
    dict(cost_basis=90), dict(entry_at=EventTime(D0, Phase.OPEN)), dict(stop_price=float('nan')),
    dict(symbol='a'), dict(entry_fee=-1)])
def test_invalid_position(kw):
    with pytest.raises(ValueError):
        replace(position(), **kw)


@pytest.mark.parametrize('kw', [dict(eligible_session=D1), dict(allocation_pct=2),
    dict(status=OrderStatus.FILLED), dict(confidence=2), dict(position_id='orphan'),
    dict(side=Side.SELL), dict(symbol='b'), dict(signal_at=OPEN)])
def test_invalid_intent(kw):
    with pytest.raises(ValueError):
        intent(**kw)


def test_snapshot_validation_and_readonly_view():
    state = PortfolioState(CLOSE, 1000., 900., 1000., positions={'A': position()}, pending={'B': intent()})
    state.validate()
    view = state.decision_view()
    state.pending.clear()
    assert 'B' in view.pending
    with pytest.raises(TypeError):
        view.positions['B'] = position()
    state.equity = 1001
    with pytest.raises(ValueError, match='reconcile'):
        state.validate()


def test_orphan_pending_and_duplicate_outcomes_rejected():
    sell = intent(symbol='A', side=Side.SELL, position_id='entry-1', allocation_pct=None, stop_price=None, target_price=None)
    state = PortfolioState(CLOSE, 1000., 1000., 1000., pending={'A': sell})
    with pytest.raises(ValueError, match='Orphan'):
        state.validate()
    outcome = OrderOutcome('expired-1', OrderStatus.EXPIRED, CLOSE, ExpirationReason.END_OF_DATASET)
    state.pending.clear()
    state.outcomes = (outcome, outcome)
    with pytest.raises(ValueError, match='Duplicate'):
        state.validate()
    assert outcome.cash_delta == 0
    assert state.cash == 1000.


def test_fill_arithmetic_and_terminal_outcomes():
    fill = Fill('entry-1', 'A', Side.BUY, SIGNAL, OPEN, 1., 100., 101., 1., 1., -102.)
    outcome = OrderOutcome('entry-1', OrderStatus.FILLED, OPEN, fill=fill)
    assert outcome.cash_delta == -102
    with pytest.raises(ValueError):
        replace(fill, cash_delta=-100)
    with pytest.raises(ValueError):
        OrderOutcome('entry-1', OrderStatus.REJECTED, OPEN, fill=fill)
    with pytest.raises(ValueError):
        OrderOutcome('entry-1', OrderStatus.PENDING, OPEN)


def test_decision_must_match_intent():
    record = DecisionRecord(CLOSE, 'B', DecisionAction.BUY, 'gate', 0., 'inputs', intent=intent())
    assert record.intent.symbol == 'B'
    with pytest.raises(ValueError):
        replace(record, action=DecisionAction.HOLD)


def test_snapshot_and_result_contracts():
    from backtesting.primary_state import PortfolioSnapshot, SimulationResult
    snapshot = PortfolioSnapshot(CLOSE, 1000., 1000., 0., ())
    result = SimulationResult(PrimarySimulationConfig(1000., .1), 'a'*64, history=[snapshot])
    assert isinstance(result.history, tuple)
    with pytest.raises(ValueError):
        replace(snapshot, exposure=1.)
    with pytest.raises(ValueError):
        replace(result, history=[snapshot, snapshot])


def test_state_rejects_future_mark_negative_cash_and_orphan_symbol():
    state = PortfolioState(CLOSE, 1000., 900., 1000., positions={'A': position()})
    state.cash = -1
    with pytest.raises(ValueError):
        state.validate()
    state.cash = 900.
    state.positions = {'WRONG': position()}
    with pytest.raises(ValueError):
        state.validate()
    with pytest.raises(ValueError):
        Mark(100., CLOSE, OPEN, MarkQuality.CURRENT)


def test_sale_cannot_have_fee_above_notional():
    with pytest.raises(ValueError, match='fee'):
        Fill('sell-1', 'A', Side.SELL, SIGNAL, OPEN, 1., 100., 100., 101., 0., -1.)

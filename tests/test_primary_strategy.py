"""Real primary evaluators on synthetic offline histories and actual fills."""
from dataclasses import replace
from datetime import date, datetime, timezone
import subprocess
import sys

import pandas as pd
import pytest

from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_ledger import PrimaryLedger
from backtesting.primary_state import (
    DecisionAction, EventTime, FactEvidence, OrderStatus, Phase, PortfolioView,
    PrimarySimulationConfig, Side, ValuationMode, ValuationObservation,
    ProvenanceStatus, ExpirationReason,
)
from backtesting.primary_strategy import CloseInputs, evaluate_close, run_primary_strategy, _technical_proxy
from engine.entry_exit.signals import evaluate_entry, evaluate_exit


HASH = 'a'*64


def value(symbol='A', **kw):
    fields = dict(symbol=symbol, filed_date=date(2023, 1, 1),
        effective_available_date=date(2023, 1, 2), fair_value=200., share_count_source='filing',
        filing_context_hash=HASH, raw_output_hash='b'*64, model_version='synthetic',
        prompt_version='synthetic', generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        required_fact_ids=('shares',),
        fact_evidence=(FactEvidence('shares', date(2023, 1, 1), 'accession', HASH),))
    return ValuationObservation(**(fields | kw))


def dataset(n=56, symbols=('A', 'B'), changes=None, missing=None, values=None, mode=ValuationMode.VERIFIED):
    dates = pd.bdate_range('2023-01-02', periods=n)
    frames = {}
    for s in symbols:
        frame = pd.DataFrame(dict(open=100., high=120., low=90., close=100., volume=1000.), index=dates)
        for (sym, i), (opening, close) in (changes or {}).items():
            if sym == s:
                frame.loc[dates[i], ['open', 'high', 'low', 'close']] = [opening, max(120, opening, close), min(90, opening, close), close]
        frames[s] = frame.drop([dates[i] for i in (missing or {}).get(s, ())])
    records = [value(s) for s in symbols] if values is None else values
    return prepare_primary_data(frames, records, session_index=dates,
        metadata=dict(calendar_source='observed_union', price_source='synthetic',
                      adjustment_policy='none', valuation_mode=mode.value))


def run(data, allocation=1., cash=1000.):
    return run_primary_strategy(data, PrimarySimulationConfig(cash, allocation, valuation_mode=data.valuation_mode))


def decision(result, symbol, day):
    return next(d for d in result.decisions if d.symbol == symbol and d.at.session == day)


def test_rejected_entry_recovery_actual_holdings_and_future_cash():
    def scenario(sale):
        return dataset(changes={('A', 52): (100, 125), ('A', 53): (sale, sale)})
    data = scenario(120)
    result = run(data)
    rejected = next(o for o in result.outcomes if o.order_id == f'B:{data.sessions[49]}:buy')
    assert rejected.status == OrderStatus.REJECTED and rejected.cash_delta == 0
    assert decision(result, 'B', data.sessions[50]).action == DecisionAction.BUY
    assert all(d.action != DecisionAction.SELL for d in result.decisions
               if d.symbol == 'B' and d.at.session < data.sessions[53])
    same_open = [f for f in result.fills if f.at.session == data.sessions[53]]
    assert [(f.symbol, f.side) for f in same_open] == [('A', Side.SELL), ('B', Side.BUY)]
    assert same_open[0].cash_delta == 1200 and same_open[1].cash_delta == -1200
    assert same_open[1].quantity == 12
    assert len([o for o in result.outcomes if o.order_id == rejected.order_id]) == 1
    other = run(scenario(200))
    cutoff = data.sessions[52]
    for attr in ('decisions', 'fills', 'outcomes', 'history'):
        assert tuple(x for x in getattr(result, attr) if x.at.session <= cutoff) == tuple(
            x for x in getattr(other, attr) if x.at.session <= cutoff)


def test_expired_exit_recovery_and_no_stale_close_signal():
    data = dataset(symbols=('A',), changes={('A', 51): (100, 80), ('A', 53): (100, 80)}, missing={'A': [52]})
    result = run(data)
    old = next(o for o in result.outcomes if o.order_id == f'A:{data.sessions[51]}:sell')
    assert old.status == OrderStatus.EXPIRED and old.reason == ExpirationReason.MISSING_EXECUTION_BAR
    d = decision(result, 'A', data.sessions[52])
    assert d.reason == 'missing_close_bar' and d.observed_quantity == 10
    assert decision(result, 'A', data.sessions[53]).action == DecisionAction.SELL
    sell = next(f for f in result.fills if f.side == Side.SELL)
    assert sell.at.session == data.sessions[54] and sell.quantity == 10


@pytest.mark.parametrize('mode,eligible', [(ValuationMode.VERIFIED, False), (ValuationMode.LEGACY, True)])
def test_strict_legacy_and_late_fact(mode, eligible):
    v = value(fact_evidence=(FactEvidence('shares', date(2024, 1, 1), 'later-restatement', HASH),))
    data = dataset(symbols=('A',), values=[v], mode=mode)
    result = run(data)
    assert bool(result.fills) == eligible
    assert data.valuations[0].provenance == ProvenanceStatus.UNVERIFIED
    assert result.config.valuation_mode == mode
    assert data.valuations[0].observation.knowledge_contamination_status == 'unverified'


def test_filing_boundary_and_future_valuation_prefix():
    dates = pd.bdate_range('2023-01-02', periods=56)
    v = value(filed_date=dates[50].date(), effective_available_date=dates[51].date())
    data = dataset(symbols=('A',), values=[v])
    result = run(data)
    assert decision(result, 'A', data.sessions[50]).reason == 'valuation_unavailable'
    assert result.fills[0].signal_at.session == data.sessions[51]
    assert result.fills[0].at.session == data.sessions[52]
    later = value(filed_date=dates[54].date(), effective_available_date=dates[55].date(), fair_value=1.)
    other = run(dataset(symbols=('A',), values=[later, v]))
    assert result.decisions[:55] == other.decisions[:55]


def test_frozen_levels_and_exit_without_valuation_or_indicators():
    data = dataset(symbols=('A',), changes={('A', 51): (100, 80)})
    ledger = PrimaryLedger(data, PrimarySimulationConfig(1000, 1))
    inputs = CloseInputs(data)
    for i in range(52):
        ledger.open_session()
        view = ledger.close_session()
        obs = inputs.at('A', view.at)
        if i == 51:
            p = view.positions['A']
            assert p.stop_price == 90 and p.target_price == 120
            obs = replace(obs, valuation=None, row={'close': 80., 'support': 1., 'resistance': 1000., 'sma_50': None})
            d = evaluate_close('A', obs, view, ledger.config)
            assert d.action == DecisionAction.SELL and d.reason == 'stop_loss'
            assert d.intent.position_id == p.entry_order_id
            assert evaluate_exit('A', p.entry_price, 80, p.stop_price, p.target_price).reason == d.reason
        else:
            d = evaluate_close('A', obs, view, ledger.config)
        ledger.submit_close([d.intent] if d.intent else [])


def test_close_next_open_and_no_intraday_exit():
    data = dataset(symbols=('A',), changes={('A', 50): (110, 100)})
    result = run(data)
    f = result.fills[0]
    assert f.signal_at.session == data.sessions[49] and f.at.session == data.sessions[50]
    assert f.price == 110 and f.quantity == pytest.approx(1000/110)
    assert len(result.fills) == 1
    assert result.terminal_positions[0].stop_price == 90
    assert result.history[-1].equity == pytest.approx(1000/110*100)


def test_pending_blocks_duplicate_without_mutation():
    data = dataset(symbols=('A',))
    config = PrimarySimulationConfig(1000, 1)
    at = EventTime(data.sessions[49], Phase.CLOSE)
    view = PortfolioView(at, 1000, 1000, 0, {}, {})
    obs = CloseInputs(data).at('A', at)
    first = evaluate_close('A', obs, view, config)
    pending = replace(view, pending={'A': first.intent})
    blocked = evaluate_close('A', obs, pending, config)
    assert blocked.reason == 'pending_order' and blocked.intent is None
    assert blocked.pending_id == first.intent.order_id
    assert not view.positions and view.cash == 1000


@pytest.mark.parametrize('field', ['sma_50', 'support', 'resistance'])
def test_missing_indicators(field):
    data = dataset(symbols=('A',))
    at = EventTime(data.sessions[49], Phase.CLOSE)
    obs = CloseInputs(data).at('A', at)
    obs = replace(obs, row=dict(obs.row) | {field: None})
    d = evaluate_close('A', obs, PortfolioView(at, 1000, 1000, 0, {}, {}), PrimarySimulationConfig(1000, 1))
    assert d.reason == 'indicator_warmup_or_missing' and d.intent is None
    assert all(d.intent is None for d in run(dataset(n=30)).decisions)


def test_multisymbol_determinism():
    one = dataset(symbols=('A', 'B'))
    two = dataset(symbols=('B', 'A'), values=[value('B'), value('A')])
    assert run(one, .6) == run(two, .6)
    result = run(one, .6)
    assert [f.quantity for f in result.fills] == [6., 2.4]
    assert result.history[-1].cash == 160


def test_final_session_and_zero_signals():
    data = dataset(n=50, symbols=('A',))
    result = run(data)
    assert not result.fills and result.decisions[-1].action == DecisionAction.BUY
    assert result.outcomes[0].reason == ExpirationReason.END_OF_DATASET
    assert result.history[-1].equity == 1000
    empty = run(dataset(values=[]))
    assert not empty.fills and not empty.outcomes
    assert all(h.cash == h.equity == 1000 for h in empty.history)


@pytest.mark.parametrize('close,fair,equity', [(100, 120, 1000), (100, 119, 1000), (99, 200, 1000), (100, 200, .5)])
def test_direct_evaluator_proxy_parity_and_current_equity(close, fair, equity):
    from backtesting.backtest_runner import _technical_proxy as original
    data = dataset(symbols=('A',), values=[value(fair_value=fair)])
    at = EventTime(data.sessions[49], Phase.CLOSE)
    obs = CloseInputs(data).at('A', at)
    obs = replace(obs, row=dict(obs.row) | {'close': close})
    view = PortfolioView(at, equity, equity, 0, {}, {})
    config = PrimarySimulationConfig(1000, 1)
    technical = original(obs.row)
    assert _technical_proxy(obs.row) == technical
    direct = evaluate_entry('A', {'estimated_fair_value_per_share': fair, 'share_count_source': 'filing'}, technical, 0., 1., equity)
    actual = evaluate_close('A', obs, view, config)
    assert (actual.intent is not None) == (direct is not None)
    if direct:
        assert actual.intent.reason == direct.reason and actual.intent.confidence == direct.confidence
        assert actual.intent.decision_equity == equity


def test_latest_invalid_valuation_does_not_resurrect_old_value():
    dates = pd.bdate_range('2023-01-02', periods=56)
    invalid = value(filed_date=dates[48].date(), effective_available_date=dates[49].date(),
                    fair_value=None, invalidation_reason='failed_research')
    result = run(dataset(symbols=('A',), values=[value(), invalid]))
    assert not result.fills
    assert result.decisions[49].reason == 'valuation_unavailable'


def test_stale_marks_block_new_buy_at_execution():
    data = dataset(missing={'A': [51]})
    result = run(data)
    outcome = next(o for o in result.outcomes if o.order_id == f'B:{data.sessions[50]}:buy')
    assert outcome.reason.value == 'stale_portfolio_marks'
    assert decision(result, 'A', data.sessions[51]).reason == 'missing_close_bar'


def test_observation_rejects_wrong_time_and_future_valuation():
    data = dataset(symbols=('A',))
    at = EventTime(data.sessions[49], Phase.CLOSE)
    obs = CloseInputs(data).at('A', at)
    with pytest.raises(ValueError):
        replace(obs, at=replace(at, phase=Phase.OPEN))
    future = replace(obs.valuation, observation=value(effective_available_date=date(2024, 1, 1)))
    with pytest.raises(ValueError, match='unavailable'):
        replace(obs, valuation=future)
    with pytest.raises(ValueError, match='match'):
        evaluate_close('A', obs, PortfolioView(EventTime(data.sessions[50], Phase.CLOSE), 1, 1, 0, {}, {}), PrimarySimulationConfig(1, 1))


def test_fresh_process_offline_no_provider_imports():
    code = '''
import socket, os, sys, importlib.abc
class DenyProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('anthropic', 'alpaca', 'db', 'engine.valuation_engine', 'engine.price_action_engine.market_data')):
            raise AssertionError('provider import: '+fullname)
sys.meta_path.insert(0, DenyProviders())
def deny(*a, **k): raise AssertionError('network call')
socket.create_connection = deny
socket.socket.connect = deny
os.environ['STARTING_CAPITAL'] = 'invalid'
os.environ['MAX_POSITION_PCT'] = 'invalid'
sys.path.insert(0, 'tests')
from test_primary_strategy import dataset, run
result = run(dataset(symbols=('A',)))
assert result.fills[0].quantity == 10
'''
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_strict_adapter_cannot_consume_legacy_eligibility():
    data = dataset(symbols=('A',), mode=ValuationMode.LEGACY,
                   values=[value(fact_evidence=(), required_fact_ids=())])
    at = EventTime(data.sessions[49], Phase.CLOSE)
    obs = CloseInputs(data).at('A', at)
    assert obs.valuation.eligible
    d = evaluate_close('A', obs, PortfolioView(at, 1000, 1000, 0, {}, {}), PrimarySimulationConfig(1000, 1))
    assert d.reason == 'valuation_unavailable'
    with pytest.raises(ValueError):
        run_primary_strategy(data, PrimarySimulationConfig(1000, 1))


def test_late_listing_and_terminal_missing_bars():
    data = dataset(n=60, missing={'B': list(range(8)), 'A': list(range(52, 60))})
    result = run(data, .5)
    assert decision(result, 'B', data.sessions[0]).reason == 'missing_close_bar'
    assert decision(result, 'B', data.sessions[56]).reason == 'indicator_warmup_or_missing'
    assert decision(result, 'B', data.sessions[57]).action == DecisionAction.BUY
    assert not any(f.symbol == 'B' for f in result.fills)  # stale A blocks execution
    assert result.terminal_positions[0].mark.quality.value == 'stale'
    assert result.history[-1].equity == 1000


def test_current_marked_equity_changes_real_entry_gate(monkeypatch):
    import backtesting.primary_strategy as strategy
    seen = []
    real = strategy.evaluate_entry
    def observe(ticker, valuation, technical, sentiment, allocation, equity):
        seen.append((ticker, equity, equity*allocation))
        return real(ticker, valuation, technical, sentiment, allocation, equity)
    monkeypatch.setattr(strategy, 'evaluate_entry', observe)
    data = dataset(changes={('A', 51): (100, 50)})
    result = run(data, .5, 2.)
    # A spends $1, B's $0.50 opening budget is rejected; A's falling mark
    # takes portfolio equity to $1.50, below the original $1 entry gate at p=.5.
    d = decision(result, 'B', data.sessions[51])
    assert d.action == DecisionAction.HOLD and d.reason == 'entry_gate_not_met'
    assert ('B', 1.5, .75) in seen
    assert evaluate_entry('B', {'estimated_fair_value_per_share': 200, 'share_count_source': 'filing'},
                         _technical_proxy({'close': 100, 'sma_50': 100, 'support': 90, 'resistance': 120}), 0, .5, 2.) is not None


def test_held_exit_precedes_flat_entry_on_same_close():
    data = dataset(symbols=('A', 'Z'), values=[value('Z'), value('A',
                   filed_date=date(2023, 3, 14), effective_available_date=date(2023, 3, 15))],
                   changes={('Z', 52): (100, 125)})
    result = run(data)
    same_close = [d for d in result.decisions if d.at.session == data.sessions[52]]
    assert [(d.symbol, d.action) for d in same_close] == [('Z', DecisionAction.SELL), ('A', DecisionAction.BUY)]


def test_entry_day_close_can_queue_exit_but_not_fill_same_day():
    data = dataset(symbols=('A',), changes={('A', 50): (100, 80), ('A', 51): (70, 80)})
    result = run(data)
    buy, sell = result.fills[:2]
    assert buy.at.session == data.sessions[50] and buy.quantity == 10
    assert sell.signal_at.session == data.sessions[50] and sell.at.session == data.sessions[51]
    assert sell.price == 70 and sell.cash_delta == 700
    assert result.closed_trades[0].exit == sell
    assert result.history[-1].equity == 700


def test_real_exit_after_valuation_invalidation_and_terminal_sell():
    dates = pd.bdate_range('2023-01-02', periods=52)
    invalid = value(filed_date=dates[50].date(), effective_available_date=dates[51].date(),
                    fair_value=None, invalidation_reason='failed_research')
    data = dataset(n=52, symbols=('A',), values=[value(), invalid], changes={('A', 51): (100, 80)})
    result = run(data)
    d = result.decisions[-1]
    assert d.action == DecisionAction.SELL and d.reason == 'stop_loss'
    assert result.outcomes[-1].reason == ExpirationReason.END_OF_DATASET
    assert result.terminal_positions[0].quantity == 10
    assert result.history[-1].equity == 800
    assert len(result.fills) == 1

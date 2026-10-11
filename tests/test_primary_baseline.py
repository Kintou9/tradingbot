"""Parity against full original module imported with external boundaries mocked."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

import pandas as pd
import pytest
import vectorbt as vbt

from backtesting.primary_baseline import isolated_baseline, run_replay_comparison, complete_comparison_data
from backtesting.primary_reporting import compare_primary_runs
from backtesting.primary_state import PrimarySimulationConfig, ValuationMode
from tests.test_primary_strategy import dataset, run, value


def config(**kw):
    return PrimarySimulationConfig(1000,.6,valuation_mode=ValuationMode.LEGACY,**kw)


def original_module(monkeypatch, data, cfg):
    # Execute the complete unchanged module, not the adapter's AST subset.
    # Only external imports and captured loaders are replaced in this test.
    def deny(*a,**k):
        raise AssertionError('External call')
    names = {'engine.valuation_engine.dcf':['run_dcf'],
             'engine.valuation_engine.fundamentals_data':['get_annual_filings','get_financial_summary_asof'],
             'engine.price_action_engine.market_data':['fetch_ohlcv']}
    modules = {}
    for name, attrs in names.items():
        module = ModuleType(name)
        for attr in attrs:
            setattr(module,attr,deny)
        modules[name]=module
    path = Path('backtesting/backtest_runner.py')
    spec = importlib.util.spec_from_file_location('phase1_direct_test',path)
    original = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules',modules), patch('dotenv.load_dotenv',lambda:None):
        spec.loader.exec_module(original)
    monkeypatch.setattr(original,'STARTING_CAPITAL',cfg.initial_cash)
    monkeypatch.setattr(original,'Limits',SimpleNamespace(from_env=lambda:SimpleNamespace(max_position_pct=cfg.allocation_pct)))
    monkeypatch.setattr(original,'os',SimpleNamespace(getenv={'BACKTEST_FEES_PCT':str(cfg.fee_pct),'BACKTEST_SLIPPAGE_PCT':str(cfg.slippage_pct)}.get))
    monkeypatch.setattr(original,'fetch_ohlcv',lambda ticker,outputsize=5000:data.prices_by_symbol[ticker][['open','high','low','close','volume']].copy())
    monkeypatch.setattr(original,'get_point_in_time_fair_values',lambda ticker,name:pd.Series({v.observation.filed_date:v.observation.fair_value if v.eligible else float('nan') for v in data.valuations if v.observation.symbol==ticker},dtype=float))
    capture={}
    def from_signals(*a,**kw):
        capture['portfolio']=vbt.Portfolio.from_signals(*a,**kw)
        return capture['portfolio']
    original.vbt=SimpleNamespace(Portfolio=SimpleNamespace(from_signals=from_signals))
    return original,capture


@pytest.mark.parametrize('fees,slippage',[(0.,0.),(.01,.02)])
def test_original_signals_orders_cash_equity_trades_and_metrics_parity(monkeypatch,fees,slippage):
    data=dataset(mode=ValuationMode.LEGACY,changes={('A',52):(100,125),('A',53):(120,120)})
    cfg=config(fee_pct=fees,slippage_pct=slippage)
    original,direct_capture=original_module(monkeypatch,data,cfg)
    adapted,capture,_=isolated_baseline(data,cfg)
    direct_signals={s:original.generate_signals(s,s) for s in data.symbols}
    signals={s:adapted['generate_signals'](s,s) for s in data.symbols}
    for symbol in data.symbols:
        for direct,other in zip(direct_signals[symbol],signals[symbol]):
            pd.testing.assert_series_equal(direct,other)
    direct_report=original.run_shared_capital_backtest(direct_signals,initial_cash=1000)
    adapted_report=adapted['run_shared_capital_backtest'](signals,initial_cash=1000)
    assert direct_report==adapted_report
    direct,p=direct_capture['portfolio'],capture['portfolio']
    pd.testing.assert_frame_equal(direct.orders.records,p.orders.records)
    pd.testing.assert_frame_equal(direct.trades.records,p.trades.records)
    pd.testing.assert_series_equal(direct.cash(),p.cash())
    pd.testing.assert_series_equal(direct.value(),p.value())
    pd.testing.assert_frame_equal(direct.assets(),p.assets())
    first=p.orders.records.iloc[0]
    assert p.wrapper.index[int(first['idx'])].date()==data.sessions[50]
    assert first['price']==pytest.approx(100*(1+slippage))
    report=run_replay_comparison(data,cfg)
    assert report['summary']['ending_equity']==float(direct.value().iloc[-1])
    assert report['summary']['total_return_pct']==direct_report['total_return_pct']


def test_known_assumed_position_divergence_preserved():
    data=dataset(mode=ValuationMode.LEGACY,changes={('A',52):(100,125),('A',53):(120,120)})
    cfg=PrimarySimulationConfig(1000,1,valuation_mode=ValuationMode.LEGACY)
    baseline=run_replay_comparison(data,cfg)
    coupled=run(data)
    assert baseline['signals']['B']['entries']==[data.sessions[50].isoformat()]
    assert not any(f['symbol']=='B' for f in baseline['fills'])
    assert any(f.symbol=='B' and f.at.session==data.sessions[53] for f in coupled.fills)
    comparison=compare_primary_runs(baseline,coupled)
    assert all(comparison['equivalence'].values())
    assert comparison['first_differing_fill'] is not None
    assert comparison['attribution']=='unattributed'


def test_non_equivalent_mode_not_misattributed():
    legacy=dataset(mode=ValuationMode.LEGACY)
    baseline=run_replay_comparison(legacy,config())
    comparison=compare_primary_runs(baseline,run(dataset(),.6))
    assert comparison['equivalence']['valuation_mode'] is False
    assert comparison['equivalence']['input_data'] is False
    assert comparison['attribution']=='unattributed'


def test_common_complete_slice_recomputes_both_inputs():
    data=dataset(mode=ValuationMode.LEGACY,missing={'B':[10]})
    common,manifest=complete_comparison_data(data)
    assert len(common.sessions)==55 and manifest['excluded_sessions']==[data.sessions[10].isoformat()]
    assert all(tuple(f.dates)==common.sessions for f in common.frames)
    baseline=run_replay_comparison(common,config())
    comparison=compare_primary_runs(baseline,run(common,.6),manifest)
    assert comparison['equivalence']['input_data'] is True


def test_baseline_explicit_legacy_and_delayed_boundary_rejected():
    with pytest.raises(ValueError,match='legacy'):
        run_replay_comparison(dataset(),config())
    dates=pd.bdate_range('2023-01-02',periods=56)
    data=dataset(mode=ValuationMode.LEGACY,values=[value(effective_available_date=dates[3].date())])
    with pytest.raises(ValueError,match='delayed'):
        run_replay_comparison(data,config())

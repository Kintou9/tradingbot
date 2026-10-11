"""Analytical reporting and corruption rejection; no performance claims."""
from dataclasses import replace
import json
import math

import pytest

from backtesting.primary_reporting import summarize_primary, serialize_report, ReportingError, fingerprint
from backtesting.primary_state import PrimarySimulationConfig, ValuationMode
from backtesting.primary_strategy import run_primary_strategy
from tests.test_primary_strategy import dataset, run


def test_zero_trades_null_statistics_and_no_metadata_leak():
    data = dataset(values=[])
    result = replace(run(data), manifest_json='{"api_key":"secret", "path":"/private/local"}')
    report = summarize_primary(result, data)
    s = report['summary']
    assert s['ending_equity'] == 1000 and s['net_pnl'] == 0
    assert s['win_rate_pct'] is None and s['profit_factor'] is None
    assert s['sharpe_ratio'] is None and s['sharpe_undefined_reason']=='zero_variance'
    assert s['fill_count']==s['closed_trade_count']==s['open_position_count']==0
    assert 'secret' not in serialize_report(report) and '/private/local' not in serialize_report(report)


def test_open_position_costs_and_cash_reconcile():
    data = dataset(symbols=('A',))
    cfg = PrimarySimulationConfig(1000,.5,fee_pct=.01,slippage_pct=.01)
    result = run_primary_strategy(data,cfg)
    report = summarize_primary(result,data)
    s = report['summary']
    q = 500/(101*1.01)
    assert s['ending_cash']==pytest.approx(500)
    assert s['ending_equity']==pytest.approx(500+q*100)
    assert s['unrealized_net_pnl']==pytest.approx(q*100-500)
    assert s['fees_paid']==pytest.approx(q*101*.01)
    assert s['slippage_costs']==pytest.approx(q)
    assert s['gross_pnl']-s['fees_paid']-s['slippage_costs']==pytest.approx(s['net_pnl'])
    assert s['closed_trade_count']==0 and s['open_position_count']==1
    assert s['win_rate_pct'] is None and s['max_drawdown_pct']==pytest.approx((1000-s['ending_equity'])/10)
    assert report['portfolio_history'][-1]['position_count']==1
    assert report['per_symbol']['A']['net_pnl']==pytest.approx(s['net_pnl'])


def test_closed_net_profit_fees_and_daily_realized():
    data = dataset(n=53,symbols=('A',),changes={('A',51):(100,125),('A',52):(120,120)})
    cfg = PrimarySimulationConfig(1000,.5,fee_pct=.01,slippage_pct=.01)
    result = run_primary_strategy(data,cfg)
    report = summarize_primary(result,data)
    q = 500/(101*1.01)
    net = q*118.8*.99-500
    s = report['summary']
    assert s['realized_net_pnl']==pytest.approx(net)
    assert s['ending_equity']==pytest.approx(1000+net)
    assert s['unrealized_net_pnl']==0
    assert s['closed_trade_count']==1 and s['win_rate_pct']==100
    assert s['average_closed_net_pnl']==pytest.approx(net)
    assert s['profit_factor'] is None
    assert s['buy_fill_count']==s['sell_fill_count']==1
    assert report['closed_trades'][0]['return_pct']==pytest.approx(100*net/500)
    assert report['portfolio_history'][-1]['realized_net_pnl']==pytest.approx(net)


def test_rejected_expired_orders_are_not_fills():
    data = dataset()
    report = summarize_primary(run(data),data)
    s = report['summary']
    assert s['fill_count']==1 and s['rejected_order_count']==6 and s['expired_order_count']==1
    for o in report['orders']:
        if o['status']!='filled':
            assert o['cash_impact']==0 and o['filled_quantity']==0 and o['actual_execution_session'] is None
    assert report['orders'][0]['symbol']=='A' and report['orders'][1]['symbol']=='B'


def test_drawdown_marks_and_loss_profit_factor():
    data = dataset(n=52,symbols=('A',),changes={('A',50):(100,80),('A',51):(70,80)})
    s = summarize_primary(run(data),data)['summary']
    assert s['max_drawdown_pct']==pytest.approx(30)
    assert s['realized_net_pnl']==-300 and s['profit_factor']==0
    assert s['win_rate_pct']==0 and s['closed_trade_count']==1


def test_stale_marks_are_preserved():
    data = dataset(missing={'A':[51,52]})
    report = summarize_primary(run(data),data)
    point = report['portfolio_history'][51]
    assert point['stale_symbols']==['A'] and point['equity']==1000
    assert point['marks']['A']['observed_at']['session']==data.sessions[50].isoformat()
    assert report['data_quality']['stale_session_count']==2


@pytest.mark.parametrize('mode',list(ValuationMode))
def test_provenance_and_byte_determinism(mode):
    data = dataset(mode=mode)
    report = summarize_primary(run(data),data)
    second = summarize_primary(run(dataset(symbols=('B','A'),mode=mode)),data)
    assert serialize_report(report)==serialize_report(second)
    assert fingerprint(report)==report['report_fingerprint']
    assert report['config']['valuation_mode']==mode.value
    assert report['data_quality']['strict_point_in_time_certified'] is False
    if mode==ValuationMode.LEGACY:
        assert report['data_quality']['valuation_label']=='historically unverified'
    assert json.loads(serialize_report(report))==report


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-float('inf')])
def test_nonfinite_serialization_rejected(value):
    with pytest.raises(ValueError):
        serialize_report({'schema_version':'test','metric':value})


@pytest.mark.parametrize('corruption',['cash','fills','closed','terminal','outcomes'])
def test_report_independently_rejects_corruption(corruption):
    data = dataset(n=53,symbols=('A',),changes={('A',51):(100,125),('A',52):(120,120)})
    result = run(data)
    if corruption=='cash':
        h = result.history[-1]
        result = replace(result,history=result.history[:-1]+(replace(h,cash=h.cash+1,equity=h.equity+1),))
    elif corruption=='fills':
        result = replace(result,fills=result.fills[:-1])
    elif corruption=='closed':
        result = replace(result,closed_trades=())
    elif corruption=='terminal':
        data = dataset(symbols=('A',))
        result = replace(run(data),terminal_positions=())
    else:
        result = replace(result,outcomes=result.outcomes[:-1])
    with pytest.raises(ReportingError):
        summarize_primary(result,data)


def test_fingerprint_cannot_hide_changed_report():
    report = summarize_primary(run(dataset()))
    report['summary']['ending_equity'] += 1
    with pytest.raises(ReportingError,match='fingerprint'):
        serialize_report(report)


def test_sample_sharpe_matches_explicit_close_return_formula():
    import statistics
    data=dataset(n=54,symbols=('A',),changes={('A',51):(100,110),('A',52):(100,105)})
    report=summarize_primary(run(data),data)
    curve=[1000.]+[p['equity'] for p in report['portfolio_history']]
    returns=[b/a-1 for a,b in zip(curve,curve[1:])]
    expected=statistics.mean(returns)/statistics.stdev(returns)*math.sqrt(252)
    assert report['summary']['sharpe_ratio']==pytest.approx(expected)
    assert report['summary']['session_return_count']==54


def test_break_even_is_not_win_and_no_loss_profit_factor_undefined():
    from backtesting.primary_ledger import PrimaryLedger
    from backtesting.primary_state import Side
    from tests.test_primary_ledger import dataset as ledger_data, order
    data=ledger_data(n=3,symbols=('A',))
    ledger=PrimaryLedger(data,PrimarySimulationConfig(1000,1))
    ledger.open_session()
    view=ledger.close_session()
    ledger.submit_close([order(ledger,view)])
    ledger.open_session()
    view=ledger.close_session()
    ledger.submit_close([order(ledger,view,side=Side.SELL,order_id='sell-A')])
    ledger.open_session()
    ledger.close_session()
    report=summarize_primary(ledger.finish(),data)
    assert report['summary']['win_rate_pct']==0
    assert report['summary']['average_closed_net_pnl']==0
    assert report['summary']['profit_factor'] is None


def test_single_session_sharpe_undefined_and_initial_equity_preserved():
    data=dataset(n=1,values=[])
    report=summarize_primary(run(data),data)
    assert report['summary']['sharpe_ratio'] is None
    assert report['summary']['sharpe_undefined_reason']=='fewer_than_two_returns'
    assert report['summary']['ending_equity']==1000

"""Independent adversarial checks for Phase 1; synthetic data, no services."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from backtesting import backtest_runner as br
from backtesting import swing_backtest as sb
from engine.swing_strategy.config import SwingStrategyConfig


def bars(n=7):
    return pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100.},
                        index=pd.bdate_range('2024-01-02', periods=n))


def candidate(df, entry=1, stop=95., target=120.):
    return (entry-1, dict(entry_i=entry, entry_date=df.index[entry], entry_price=100.,
                         stop_price=stop, target_price=target, reward_to_risk=(target-100)/(100-stop)))


def test_future_exit_cannot_release_risk_for_an_earlier_entry():
    a, b = bars(), bars()
    a.loc[a.index[5], ['high', 'close']] = [121., 120.]
    cfg = SwingStrategyConfig(risk_per_trade_pct=.01, max_total_open_risk_pct=.01, slippage_bps=0)
    trades, evaluated, rejected, cash, risk = sb._simulate_tickers(
        {'A': a, 'B': b}, {'A': [candidate(a)], 'B': [candidate(b, entry=2, target=200)]}, cfg, 10000.)
    assert evaluated['B'] == 1
    assert rejected['B'] == 1, 'A does not exit until session 5; its risk must still be reserved on session 2'
    assert risk == 0
    assert cash == pytest.approx(10400.)


def test_sizing_uses_equity_not_remaining_cash():
    a, b = bars(), bars()
    cfg = SwingStrategyConfig(risk_per_trade_pct=.01, max_total_open_risk_pct=.10, slippage_bps=0)
    result = sb._simulate_tickers({'A': a, 'B': b},
        {'A': [candidate(a, target=200)], 'B': [candidate(b, entry=2, target=200)]}, cfg, 10000.)
    assert result[4] == pytest.approx(200.), 'Two flat-price holdings each risk 1% of unchanged $10,000 equity'


def test_entry_session_stop_is_not_skipped():
    df = bars()
    df.loc[df.index[1], 'low'] = 94.
    result = sb._simulate_tickers({'A': df}, {'A': [candidate(df)]},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000.)
    assert len(result[0]) == 1
    assert result[0][0].exit_date == str(df.index[1].date())
    assert result[0][0].pnl == pytest.approx(-100.)


def test_unverified_dcf_is_not_promoted_to_filing_provenance(monkeypatch):
    monkeypatch.setattr(br, 'get_annual_filings', lambda _: [{'filed': '2024-01-01'}])
    monkeypatch.setattr(br, 'get_financial_summary_asof', lambda *a, **kw: 'filing without a usable share count')
    monkeypatch.setattr(br, 'run_dcf', lambda *a, **kw: {'structured': {
        'estimated_fair_value_per_share': 1000., 'share_count_source': 'unavailable'}})
    values = br.get_point_in_time_fair_values('A', 'Company A')
    assert values.dropna().empty, 'A non-null fair value alone is not proof of a filing-sourced share count'


def test_same_day_filing_is_not_available_at_session_open():
    values = pd.Series({'2024-01-02': 200.})
    assert sb._dcf_structured_as_of(values, pd.Timestamp('2024-01-02')) is None
    assert sb._dcf_structured_as_of(values, pd.Timestamp('2024-01-03'))['share_count_source'] == 'filing'


def test_open_positions_are_included_in_terminal_equity_and_drawdown():
    df = bars()
    df.loc[df.index[-1], ['high', 'close']] = [111., 110.]
    result = sb._simulate_tickers({'A': df}, {'A': [candidate(df, target=200)]},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000.)
    assert hasattr(result, 'equity_curve'), 'Closed-trade-only P&L omits held assets'
    assert result.equity_curve.iloc[-1] == pytest.approx(10200.)
    assert result.cash + result.positions_value == pytest.approx(result.equity_curve.iloc[-1])


def test_vectorbt_trade_statistics_exclude_unrealized_open_trades():
    idx = pd.bdate_range('2024-01-02', periods=3)
    p = pd.Series([100., 110., 120.], index=idx)
    pf = br.run_backtest(p, pd.Series([True, False, False], index=idx),
        pd.Series(False, index=idx), initial_cash=1000., max_position_pct=.5, fees=0, slippage=0)
    report = br.summarize(pf)
    assert report['win_rate_pct'] is None, 'An unrealized position is not a completed winning trade'
    assert report['num_closed_trades'] == 0


@pytest.mark.parametrize('kwargs', [{'initial_cash': -1}, {'fees': -.01}, {'slippage': float('nan')}, {'max_position_pct': 2}])
def test_invalid_backtest_economics_fail_closed(kwargs):
    p = pd.Series([100., 100.], index=pd.bdate_range('2024-01-02', periods=2))
    with pytest.raises(ValueError):
        br.run_backtest(p, pd.Series([True, False], index=p.index), pd.Series(False, index=p.index), **kwargs)


def test_later_price_changes_do_not_change_earlier_equity_or_allocations():
    a, b = bars(), bars()
    cfg = SwingStrategyConfig(risk_per_trade_pct=.01, max_total_open_risk_pct=.04, slippage_bps=0)
    raws = {'A': [candidate(a, target=200)], 'B': [candidate(b, entry=2, target=200)]}
    before = sb._simulate_tickers({'A': a, 'B': b}, raws, cfg, 10000.)
    a.loc[a.index[-1], ['high', 'close']] = [250., 240.]
    after = sb._simulate_tickers({'A': a, 'B': b}, raws, cfg, 10000.)
    pd.testing.assert_series_equal(before.equity_curve.iloc[:-1], after.equity_curve.iloc[:-1])
    pd.testing.assert_series_equal(before.cash_curve.iloc[:-1], after.cash_curve.iloc[:-1])


def test_same_session_exit_cash_is_not_spent_before_intraday_exit():
    a, b = bars(), bars()
    a.loc[a.index[2], ['high', 'close']] = [121., 120.]
    cfg = SwingStrategyConfig(risk_per_trade_pct=.01, max_total_open_risk_pct=.01, slippage_bps=0)
    result = sb._simulate_tickers({'A': a, 'B': b},
        {'A': [candidate(a)], 'B': [candidate(b, entry=2)]}, cfg, 10000.)
    assert result.rejected['B'] == 1
    assert result.cash == pytest.approx(10400.)


def test_swing_fee_and_slippage_cash_reconcile_exactly():
    df = bars()
    df.loc[df.index[2], ['high', 'close']] = [121., 120.]
    result = sb._simulate_tickers({'A': df}, {'A': [candidate(df)]},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=50), 10000., fees=.001)
    trade = result.trades[0]
    entry = 20*100*1.005
    exit_ = 20*120*.995
    net = exit_*(1-.001)-entry*(1+.001)
    assert trade.shares == 20
    assert trade.pnl == pytest.approx(net)
    assert trade.gross_pnl-trade.fees_paid-trade.slippage_cost == pytest.approx(net)
    assert result.cash == pytest.approx(10000+net)
    assert result.equity_curve.iloc[-1] == pytest.approx(10000+net)
    assert result.fees_paid == pytest.approx((entry+exit_)*.001)
    assert result.cash_curve.min() >= 0


def test_fees_cannot_overdraw_cash_even_when_live_sizer_estimate_fits():
    df = bars()
    cfg = SwingStrategyConfig(risk_per_trade_pct=.05, max_total_open_risk_pct=.1, slippage_bps=0)
    result = sb._simulate_tickers({'A': df}, {'A': [candidate(df)]}, cfg, 1000., fees=.001)
    # 10 shares at $100 use all cash before fees: must reject, not borrow.
    assert result.rejected['A'] == 1
    assert result.cash == 1000


def test_intraday_drawdown_on_unclosed_position_is_reported():
    df = bars()
    df.loc[df.index[3], ['open', 'high', 'low', 'close']] = [100, 100, 91, 92]
    result = sb._simulate_tickers({'A': df}, {'A': [candidate(df, stop=90, target=200)]},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000.)
    report = sb._summarize(result.trades, 10000, result)
    assert report['trade_count'] == 0
    assert report['max_drawdown_pct'] == pytest.approx(.8)
    assert report['portfolio_return_pct'] == 0
    assert report['open_position_count'] == 1
    json.dumps(report, allow_nan=False)


def test_late_listing_has_no_prefilled_position_and_no_future_marks():
    a = bars()
    b = bars().iloc[3:].copy()
    result = sb._simulate_tickers({'A': a, 'B': b},
        {'A': [], 'B': [candidate(b, target=200)]}, SwingStrategyConfig(slippage_bps=0), 10000.)
    assert (result.cash_curve.loc[:a.index[3]] == 10000).all()
    assert result.equity_curve.iloc[-1] == 10000


def test_window_simulations_do_not_import_positions_or_profits_from_before_window():
    df = bars(8)
    df.loc[df.index[5], ['high', 'close']] = [121., 120.]
    report = sb.report_prepared({'A': df}, {'A': [candidate(df)]},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000., n_windows=2)
    assert report['shared_capital_portfolio']['trade_count'] == 1
    second = report['windows'][1]
    assert second['trade_count'] == 0
    assert second['ending_equity'] == 10000
    assert second['open_position_count'] == 0


def test_invalid_latest_filing_invalidates_prior_fair_value(monkeypatch):
    monkeypatch.setattr(br, 'get_annual_filings', lambda _: [{'filed': '2024-01-01'}, {'filed': '2024-02-01'}])
    monkeypatch.setattr(br, 'get_financial_summary_asof', lambda *a, **kw: kw['as_of'])
    monkeypatch.setattr(br, 'run_dcf', lambda *a, **kw: {'structured': {
        'estimated_fair_value_per_share': 200.,
        'share_count_source': 'filing' if kw['filing_context'] == '2024-01-01' else 'unavailable'}})
    values = br.get_point_in_time_fair_values('A', 'A')
    assert br.fair_value_asof(values, pd.Timestamp('2024-01-03')) == 200
    assert br.fair_value_asof(values, pd.Timestamp('2024-02-02')) is None


def test_open_execution_and_close_valuation_are_separate():
    idx = pd.bdate_range('2024-01-02', periods=3)
    opens = pd.Series([100., 100., 100.], index=idx)
    closes = pd.Series([100., 110., 120.], index=idx)
    opens.attrs['close_data'] = closes
    pf = br.run_backtest(opens, pd.Series([False, True, False], index=idx),
        pd.Series(False, index=idx), initial_cash=1000., max_position_pct=.5, fees=0, slippage=0)
    assert pf.assets().iloc[1] == 5
    assert pf.value().iloc[-1] == 1100
    report = br.summarize_ticker_backtest('A', opens, pd.Series([False, True, False], index=idx),
        pd.Series(False, index=idx), initial_cash=1000., n_windows=2)
    assert report['windows'][1]['starting_equity'] == pytest.approx(report['windows'][0]['ending_equity'])


def test_reported_cost_settings_match_actual_portfolio():
    p = pd.Series([100., 110.], index=pd.bdate_range('2024-01-02', periods=2))
    pf = br.run_backtest(p, pd.Series([True, False], index=p.index), pd.Series([False, True], index=p.index),
                        initial_cash=1000, fees=.002, slippage=.003)
    assert br.summarize(pf)['fees_pct'] == .002
    assert br.summarize(pf)['slippage_pct'] == .003


def test_comparisons_reuse_data_and_expose_common_cohort_and_windows(monkeypatch):
    calls = []
    df = bars(8)
    def prepare(ticker, *args):
        calls.append(ticker)
        return (df, []), None
    monkeypatch.setattr(sb, '_prepare_ticker', prepare)
    monkeypatch.setattr(sb, 'get_point_in_time_fair_values', lambda *a: pd.Series({'2023-01-01': 200.}))
    monkeypatch.setattr(sb, '_find_raw_candidates', lambda *a: [])
    report = sb.compare_variants([('A', 'A Corp'), ('B', None)], SwingStrategyConfig(), n_windows=4)
    assert calls == ['A'], 'Fetch each common ticker once, never a separate price snapshot per variant'
    for variant in ('A', 'B'):
        assert report[variant]['comparison_universe'] == ['A']
        assert 'B' in report[variant]['excluded_tickers']
        assert len(report[variant]['windows']) == 4
        assert report[variant]['shared_capital_portfolio']['ending_equity'] == 100000.
    assert report['A']['comparison_period'] == report['B']['comparison_period']
    assert not report['C']['supported'] and not report['D']['supported']
    json.dumps(report, allow_nan=False)


def test_sentiment_only_comparison_does_not_fetch_any_data(monkeypatch):
    monkeypatch.setattr(sb, '_prepare_ticker', lambda *a: pytest.fail('Unsupported variant must not fetch data'))
    result = sb.compare_variants([('A', 'A')], SwingStrategyConfig(), variants=['C', 'D'])
    assert all(not v['supported'] for v in result.values())


@pytest.mark.parametrize('n_windows', [0, -1, 2.5, True])
def test_bad_window_count_is_rejected(n_windows):
    from backtesting.metrics import split_into_windows
    with pytest.raises(ValueError):
        split_into_windows(pd.Series([1, 2, 3]), n_windows)


def test_ticker_input_order_does_not_change_shared_allocation():
    a = bars()
    b = bars()
    cfg = SwingStrategyConfig(risk_per_trade_pct=.01, max_total_open_risk_pct=.01, slippage_bps=0)
    first = sb._simulate_tickers({'A': a, 'B': b}, {'A': [candidate(a)], 'B': [candidate(b)]}, cfg, 10000.)
    second = sb._simulate_tickers({'B': b, 'A': a}, {'B': [candidate(b)], 'A': [candidate(a)]}, cfg, 10000.)
    assert first.rejected == second.rejected == {'A': 0, 'B': 1}
    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve)


def test_first_bar_cost_counts_toward_drawdown():
    p = pd.Series([100., 100.], index=pd.bdate_range('2024-01-02', periods=2))
    pf = br.run_backtest(p, pd.Series([True, False], index=p.index),
        pd.Series(False, index=p.index), initial_cash=1000., max_position_pct=1., fees=.01, slippage=0)
    assert br.summarize(pf)['max_drawdown_pct'] == pytest.approx(100 * (1 - 1/1.01))


@pytest.mark.parametrize('kind', ['duplicate', 'unsorted', 'missing_price', 'misaligned_signal'])
def test_invalid_price_history_or_signal_alignment_is_rejected(kind):
    p = pd.Series([100., 101., 102.], index=pd.bdate_range('2024-01-02', periods=3))
    if kind == 'duplicate':
        p.index = pd.DatetimeIndex([p.index[0], p.index[0], p.index[2]])
    elif kind == 'unsorted':
        p = p.iloc[::-1]
    elif kind == 'missing_price':
        p.iloc[1] = float('nan')
    entries = pd.Series(False, index=p.index)
    if kind == 'misaligned_signal':
        entries = entries.iloc[:-1]
    with pytest.raises(ValueError):
        br.run_backtest(p, entries, pd.Series(False, index=p.index), initial_cash=1000.)

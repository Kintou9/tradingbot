"""Final review checks using analytical fixtures; no external services."""
import pandas as pd
import pytest

from backtesting import backtest_runner as br
from backtesting import swing_backtest as sb
from engine.swing_strategy.config import SwingStrategyConfig


def swing_fixture(stop=95.):
    dates = pd.bdate_range('2024-01-02', periods=4)
    frame = pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100.}, index=dates)
    raw = [(0, dict(entry_i=1, entry_date=dates[1], entry_price=100.,
                    stop_price=stop, target_price=120., reward_to_risk=20/(100-stop)))]
    return frame, raw


def test_swing_preserves_fractional_share_increment_and_exact_equity():
    frame, raw = swing_fixture(stop=93.)
    result = sb._simulate_tickers({'A': frame}, {'A': raw},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000., fees=0)
    shares = result.open_positions['A']['shares']
    assert shares == pytest.approx(14.2857)
    assert result.open_risk == pytest.approx(shares*7)
    assert result.open_risk <= 100.
    assert result.cash == pytest.approx(10000.-shares*100.)
    assert result.cash + result.positions_value == pytest.approx(10000.)


def test_shared_primary_return_proves_only_remaining_cash_is_allocated(monkeypatch):
    monkeypatch.setenv('MAX_POSITION_PCT', '.6')
    monkeypatch.setenv('BACKTEST_FEES_PCT', '0')
    monkeypatch.setenv('BACKTEST_SLIPPAGE_PCT', '0')
    dates = pd.bdate_range('2024-01-02', periods=3)
    opens = pd.Series([100., 100., 110.], index=dates)
    entries = pd.Series([True, False, False], index=dates)
    exits = pd.Series(False, index=dates)
    result = br.run_shared_capital_backtest(
        {'B': (opens, entries, exits), 'A': (opens, entries, exits)}, initial_cash=1000.)
    # A invests $600; B invests 60% of the remaining $400 = $240.
    # A 10% appreciation gives $84, not $120 from two independent pools.
    assert result['total_return_pct'] == pytest.approx(8.4)
    assert result['num_open_trades'] == 2
    assert result['num_closed_trades'] == 0


def test_swing_gap_loss_exceeds_planned_risk_and_settles_at_open():
    frame, raw = swing_fixture()
    frame.loc[frame.index[2], ['open', 'high', 'low', 'close']] = [80., 90., 75., 85.]
    result = sb._simulate_tickers({'A': frame}, {'A': raw},
        SwingStrategyConfig(risk_per_trade_pct=.01, slippage_bps=0), 10000., fees=0)
    trade = result.trades[0]
    assert trade.exit_price == 80.
    assert trade.pnl == pytest.approx(-400.)  # planned stop risk was only $100
    assert result.cash == pytest.approx(9600.)
    assert result.open_risk == 0.
    assert result.positions_value == 0.

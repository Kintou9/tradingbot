"""Regression tests for swing_backtest.py's real position sizing, real
cash/open-risk accounting, and shared-capital simulation -- the
shares=1.0 / no-real-portfolio-accounting fixes. Hand-built candidates
and tiny synthetic OHLCV bars, bypassing the chart-pattern scanner
(already covered by test_swing_sizing.py / test_swing_exits.py) so these
tests isolate the sizing/accounting logic itself.
"""
import pandas as pd
import pytest

from engine.swing_strategy.config import SwingStrategyConfig
from backtesting.swing_backtest import SimulatedTrade, _simulate_tickers, _summarize


def make_config(**overrides) -> SwingStrategyConfig:
    return SwingStrategyConfig(**overrides)


def _bars(n=10, open_=100.0, high=100.0, low=100.0, close=100.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": [open_] * n, "high": [high] * n, "low": [low] * n, "close": [close] * n}, index=idx)


def _candidate(df, entry_i, entry_price=100.0, stop_price=95.0, target_price=120.0, reward_to_risk=2.0):
    return (entry_i - 1, {
        "entry_i": entry_i, "entry_date": df.index[entry_i], "entry_price": entry_price,
        "stop_price": stop_price, "target_price": target_price, "reward_to_risk": reward_to_risk,
    })


def test_shares_scale_with_equity_and_stop_distance_not_fixed_at_one():
    config = make_config(risk_per_trade_pct=0.01, slippage_bps=0)
    df = _bars(n=6, open_=100.0, high=130.0, low=80.0, close=100.0)
    # risk/share = 100-95=5; 1% of $100,000 = $1000 risk budget -> 200 shares, not 1.0
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=130.0)]
    df_exit = df.copy()
    df_exit.loc[df_exit.index[2], ["open", "high", "low", "close"]] = [100.0, 131.0, 99.0, 130.0]  # hits target

    trades, evaluated, rejected, cash, open_risk = _simulate_tickers(
        {"T": df_exit}, {"T": raw}, config, account_equity=100_000.0)

    assert len(trades) == 1
    assert trades[0].shares == pytest.approx(200.0), "shares must scale with risk budget / stop distance, not be fixed at 1.0"
    assert trades[0].shares != 1.0


def test_insufficient_cash_skips_candidate_instead_of_forcing_a_trade():
    # max_total_open_risk_pct generous (doesn't bind) so this isolates the
    # CASH check specifically: a tight stop (risk/share=$1) against a 50%
    # risk budget implies 500 shares at $100 = $50,000 -- far more than
    # the $1,000 actually available.
    config = make_config(risk_per_trade_pct=0.5, max_total_open_risk_pct=1.0, slippage_bps=0)
    df = _bars(n=6, open_=100.0, high=130.0, low=80.0, close=100.0)
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=99.0, target_price=130.0)]

    trades, evaluated, rejected, cash, open_risk = _simulate_tickers(
        {"T": df}, {"T": raw}, config, account_equity=1_000.0)

    assert evaluated["T"] == 1
    assert rejected["T"] == 1
    assert len(trades) == 0, "a candidate that can't be afforded must be skipped, never forced through at a fictional size"
    assert cash == 1_000.0, "cash must be untouched when a candidate is rejected"


def test_aggregate_open_risk_cap_carries_across_sequential_trades_same_ticker():
    config = make_config(risk_per_trade_pct=0.02, max_total_open_risk_pct=0.02, slippage_bps=0)
    df = _bars(n=12, open_=100.0, high=100.0, low=100.0, close=100.0)
    # First candidate opens and never exits within available bars (stop/target never touched) --
    # its open_risk should block a second candidate for the SAME ticker before it's reached
    # anyway (cursor logic already prevents a second candidate while position #1 is open), so
    # this test instead confirms the risk cap is what the *sizing* call sees, not that nothing
    # else can happen -- see the shared-capital cross-ticker test below for the cap actually
    # blocking a *different* ticker's trade.
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=200.0)]
    trades, evaluated, rejected, cash, open_risk_after = _simulate_tickers(
        {"T": df}, {"T": raw}, config, account_equity=100_000.0)
    assert len(trades) == 0  # never hits stop/target/time-exit in this flat fixture -- stays open
    assert open_risk_after == pytest.approx(2_000.0)  # risk_per_trade_pct * equity, still "open" at end of data


def test_aggregate_open_risk_cap_blocks_a_different_tickers_entry_in_shared_pool():
    config = make_config(risk_per_trade_pct=0.02, max_total_open_risk_pct=0.02, slippage_bps=0)
    df_a = _bars(n=12, open_=100.0, high=100.0, low=100.0, close=100.0)   # never exits -> stays open, holds the whole risk cap
    df_b = _bars(n=12, open_=50.0, high=50.0, low=50.0, close=50.0)

    raw_a = [_candidate(df_a, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=200.0)]
    # B's candidate is detected AFTER A's (entry_i=2 here vs A's entry_i=1) so A is sized first
    # and consumes the full 2% risk cap, leaving nothing for B in the SAME shared pool.
    raw_b = [_candidate(df_b, entry_i=2, entry_price=50.0, stop_price=47.5, target_price=100.0)]

    trades, evaluated, rejected, cash, open_risk = _simulate_tickers(
        {"A": df_a, "B": df_b}, {"A": raw_a, "B": raw_b}, config, account_equity=100_000.0)

    assert evaluated == {"A": 1, "B": 1}
    assert rejected["B"] == 1, "B's entry must be rejected -- A already used up the shared risk cap"
    assert len(trades) == 0


def test_pnl_reconciles_at_real_simulated_share_count():
    config = make_config(risk_per_trade_pct=0.01, slippage_bps=0)
    df = _bars(n=6, open_=100.0, high=100.0, low=100.0, close=100.0)
    df.loc[df.index[2], ["open", "high", "low", "close"]] = [100.0, 121.0, 99.0, 120.0]  # hits target at 120
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=120.0)]

    trades, *_ = _simulate_tickers({"T": df}, {"T": raw}, config, account_equity=100_000.0)

    assert len(trades) == 1
    t = trades[0]
    # risk/share=5, 1% of 100k=$1000 budget -> 200 shares
    assert t.shares == pytest.approx(200.0)
    assert t.gross_pnl == pytest.approx(t.shares * (t.exit_price - t.entry_price))
    assert t.pnl == pytest.approx(t.gross_pnl)  # slippage_bps=0 here -> net equals gross


def test_slippage_reduces_net_pnl_versus_gross():
    df = _bars(n=6, open_=100.0, high=100.0, low=100.0, close=100.0)
    df.loc[df.index[2], ["open", "high", "low", "close"]] = [100.0, 121.0, 99.0, 120.0]
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=120.0)]

    no_slip = make_config(risk_per_trade_pct=0.01, slippage_bps=0)
    with_slip = make_config(risk_per_trade_pct=0.01, slippage_bps=50)  # 0.5% each way

    trades_a, *_ = _simulate_tickers({"T": df}, {"T": raw}, no_slip, account_equity=100_000.0)
    trades_b, *_ = _simulate_tickers({"T": df}, {"T": raw}, with_slip, account_equity=100_000.0)

    assert trades_a[0].pnl > trades_b[0].pnl, "slippage must reduce net pnl relative to the zero-cost baseline"
    assert trades_b[0].gross_pnl == pytest.approx(trades_a[0].gross_pnl), "gross pnl must be unaffected by slippage"


def test_gap_and_same_bar_stop_target_policy_still_applied_unchanged():
    # Regression guard: evaluate_swing_exit_bar's own conservative policy
    # (gap resolved at the open; an ambiguous same-bar double-touch
    # resolves to the stop, never the target) must still be exactly what
    # the simulator defers to -- this is not new behavior, just confirming
    # the sizing rewrite didn't disturb it.
    config = make_config(risk_per_trade_pct=0.01, slippage_bps=0)
    df = _bars(n=6, open_=100.0, high=100.0, low=100.0, close=100.0)
    # Bar touches both stop (95) and target (120) intrabar -> must resolve to STOP.
    df.loc[df.index[2], ["open", "high", "low", "close"]] = [100.0, 121.0, 94.0, 110.0]
    raw = [_candidate(df, entry_i=1, entry_price=100.0, stop_price=95.0, target_price=120.0)]

    trades, *_ = _simulate_tickers({"T": df}, {"T": raw}, config, account_equity=100_000.0)

    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"


def test_summarize_reports_gross_net_and_slippage_cost_separately():
    trades = [
        SimulatedTrade(ticker="T", entry_date="2024-01-01", entry_price=100.0, stop_price=95.0,
                        target_price=120.0, reward_to_risk=2.0, shares=10.0, exit_date="2024-01-03",
                        exit_price=120.0, exit_reason="take_profit", sessions_held=2,
                        pnl=195.0, pnl_pct=0.195, gross_pnl=200.0),
    ]
    result = _summarize(trades, account_equity=100_000.0)
    assert result["gross_pnl_total"] == pytest.approx(200.0)
    assert result["net_pnl_total"] == pytest.approx(195.0)
    assert result["total_slippage_cost"] == pytest.approx(5.0)


def test_summarize_empty_trades_returns_none_not_zero_or_crash():
    result = _summarize([], account_equity=100_000.0)
    assert result["trade_count"] == 0
    assert result["win_rate_pct"] is None
    assert result["gross_pnl_total"] is None

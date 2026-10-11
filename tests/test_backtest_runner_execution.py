"""Regression tests for backtest_runner.py's execution-timing, sizing,
fee/slippage, and degenerate-metric fixes. Hand-built synthetic data,
no network, no DB, no LLM calls -- matches tests/test_swing_sizing.py's
plain-function style.
"""
import math

import pandas as pd
import pytest

from backtesting import backtest_runner as br
from backtesting.metrics import clean_metric, split_into_windows


def test_run_backtest_fills_at_next_bar_open_not_signal_close():
    # Day0's close is irrelevant to the fill -- day1's open ($150) is
    # deliberately far from day0's close ($101) so a same-close bug
    # would produce a visibly different (wrong) invested amount.
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    opens = pd.Series([100.0, 150.0, 150.0, 150.0], index=idx)
    naive_entries = pd.Series([True, False, False, False], index=idx)  # detected at day0
    exits = pd.Series([False, False, False, False], index=idx)

    # generate_signals' own convention: shift the detected signal forward
    # one bar before handing it to vectorbt, and price against `open`.
    shifted_entries = naive_entries.shift(1, fill_value=False)

    portfolio = br.run_backtest(opens, shifted_entries, exits, initial_cash=1000.0,
                                 max_position_pct=0.5, fees=0.0, slippage=0.0)

    assert portfolio.assets().iloc[0] == 0, "nothing should fill on the detection bar itself"
    shares = portfolio.assets().iloc[1]
    assert shares > 0, "should fill on the very next bar"
    assert shares * 150.0 == pytest.approx(500.0), "50% of $1000 priced at day1's OPEN, not day0's close"


def test_generate_signals_returns_open_series_and_shifts_entries(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    opens = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    closes = [11.0, 21.0, 31.0, 41.0, 51.0, 61.0]
    raw_df = pd.DataFrame({"open": opens, "high": closes, "low": opens, "close": closes}, index=idx)

    monkeypatch.setattr(br, "fetch_ohlcv", lambda ticker, outputsize=5000: raw_df.copy())
    # Stand in for the real pandas_ta indicator calc (which needs 50+
    # rows of warmup) -- always reads as an uptrend so _technical_proxy
    # never blocks the signal in this fixture.
    monkeypatch.setattr(br, "compute_indicators", lambda d: d.assign(sma_50=d["close"] - 1))
    monkeypatch.setattr(br, "rolling_support_resistance", lambda d: pd.DataFrame(
        {"support": d["low"], "resistance": d["high"]}, index=d.index))
    # A fair value far above every close -> clears the 20% discount gate
    # on every eligible bar.
    monkeypatch.setattr(br, "get_point_in_time_fair_values", lambda ticker, name: pd.Series(
        {idx[0].strftime("%Y-%m-%d"): 1000.0}))

    price, entries, exits = br.generate_signals("TEST", "Test Co")

    assert price.equals(raw_df["open"]), "fill-price series must be OPEN, never CLOSE"
    assert entries.iloc[0] == False, "a signal can never fire on the very first bar after shifting"
    assert entries.any(), "fixture is tuned to clear every gate -- expected at least one real entry"


def test_degenerate_backtest_reports_none_not_inf_or_nan():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    price = pd.Series([100.0] * 5, index=idx)
    no_signal = pd.Series([False] * 5, index=idx)

    portfolio = br.run_backtest(price, no_signal, no_signal, initial_cash=1000.0,
                                 max_position_pct=0.1, fees=0.0, slippage=0.0)
    result = br.summarize(portfolio)

    assert result["num_trades"] == 0
    assert result["sharpe_ratio"] is None
    assert result["win_rate_pct"] is None
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe_ratio", "win_rate_pct"):
        assert result[key] is None or math.isfinite(result[key])


def test_clean_metric_guards_non_finite_values():
    assert clean_metric(float("inf")) is None
    assert clean_metric(float("nan")) is None
    assert clean_metric(None) is None
    assert clean_metric(5.0) == 5.0
    assert clean_metric(0.0) == 0.0  # a real, meaningful zero must survive


def test_fees_and_slippage_reduce_net_return_when_configured():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    price = pd.Series([100.0, 100.0, 110.0], index=idx)
    entries = pd.Series([True, False, False], index=idx)
    exits = pd.Series([False, False, True], index=idx)

    free = br.run_backtest(price, entries, exits, initial_cash=1000.0,
                            max_position_pct=1.0, fees=0.0, slippage=0.0)
    costly = br.run_backtest(price, entries, exits, initial_cash=1000.0,
                              max_position_pct=1.0, fees=0.01, slippage=0.01)

    assert costly.total_return() < free.total_return(), "fees+slippage must reduce net return vs. the free baseline"


def test_fees_and_slippage_default_to_zero_when_unset(monkeypatch):
    monkeypatch.delenv("BACKTEST_FEES_PCT", raising=False)
    monkeypatch.delenv("BACKTEST_SLIPPAGE_PCT", raising=False)
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    price = pd.Series([100.0, 100.0], index=idx)
    flat = pd.Series([False, False], index=idx)
    portfolio = br.run_backtest(price, flat, flat, initial_cash=1000.0, max_position_pct=0.1)
    result = br.summarize(portfolio)
    assert result["fees_pct"] == 0.0
    assert result["slippage_pct"] == 0.0


def test_max_position_pct_caps_simulated_fill_size():
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    price = pd.Series([100.0, 100.0], index=idx)
    entries = pd.Series([True, False], index=idx)
    exits = pd.Series([False, False], index=idx)

    small = br.run_backtest(price, entries, exits, initial_cash=1000.0, max_position_pct=0.1, fees=0.0, slippage=0.0)
    big = br.run_backtest(price, entries, exits, initial_cash=1000.0, max_position_pct=0.5, fees=0.0, slippage=0.0)

    assert small.assets().iloc[0] * 100.0 == pytest.approx(100.0)   # 10% of $1000
    assert big.assets().iloc[0] * 100.0 == pytest.approx(500.0)     # 50% of $1000


def test_split_into_windows_returns_requested_non_overlapping_count():
    idx = pd.date_range("2024-01-01", periods=20, freq="D")
    data = pd.Series(range(20), index=idx)

    windows = split_into_windows(data, n_windows=4)

    assert len(windows) == 4
    assert sum(len(w) for w in windows) == 20
    seen = set()
    for w in windows:
        assert seen.isdisjoint(w.index), "windows must not overlap"
        seen.update(w.index)


def test_split_into_windows_rejects_invalid_count():
    with pytest.raises(ValueError):
        split_into_windows(pd.Series([1, 2, 3]), n_windows=0)


def test_shared_capital_backtest_makes_tickers_compete_for_one_pool(monkeypatch):
    # 60% sizing each: independent runs would each invest 60% of a FULL
    # $1000 ($600 apiece, $1200 total -- impossible). A real shared pool
    # must still fill both (proving it isn't just blocking the second
    # one outright) while visibly constraining the second fill.
    monkeypatch.setenv("MAX_POSITION_PCT", "0.6")
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    price = pd.Series([100.0] * 5, index=idx)
    entries = pd.Series([True, False, False, False, False], index=idx)
    no_exits = pd.Series([False] * 5, index=idx)

    signals = {"A": (price, entries, no_exits), "B": (price, entries, no_exits)}
    result = br.run_shared_capital_backtest(signals, initial_cash=1000.0)

    assert result["supported"] is True
    assert set(result["tickers"]) == {"A", "B"}
    assert result["num_trades"] == 2, "both A and B should still get a fill from the shared pool"
    # Price never moves in this fixture -- with no gains/losses, a
    # correctly shared (non-negative, non-overdrawn) cash pool returns
    # exactly flat, not some fabricated profit from double-counted cash.
    assert result["total_return_pct"] == pytest.approx(0.0)


def test_shared_capital_backtest_empty_input_is_marked_unsupported():
    result = br.run_shared_capital_backtest({})
    assert result["supported"] is False


def test_shared_capital_backtest_no_overlap_is_marked_unsupported():
    idx_a = pd.date_range("2020-01-01", periods=3, freq="D")
    idx_b = pd.date_range("2024-01-01", periods=3, freq="D")
    flat_a = pd.Series([False] * 3, index=idx_a)
    flat_b = pd.Series([False] * 3, index=idx_b)
    signals = {
        "A": (pd.Series([100.0] * 3, index=idx_a), flat_a, flat_a),
        "B": (pd.Series([100.0] * 3, index=idx_b), flat_b, flat_b),
    }
    result = br.run_shared_capital_backtest(signals)
    assert result["supported"] is False

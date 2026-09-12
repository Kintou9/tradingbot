import pandas as pd

from engine.swing_strategy.config import SwingStrategyConfig
from engine.swing_strategy.technical_trigger import (
    find_pullback_setup, check_confirmation, check_confirmation_intraday,
)


def _config(**overrides) -> SwingStrategyConfig:
    return SwingStrategyConfig(trend_sma_length=5, trend_slope_lookback_sessions=3,
                                pullback_sma_length=3, confirmation_buffer_pct=0.0, **overrides)


def _bars(rows):
    """rows: list of dicts with open/high/low/close/sma_50/sma_20/resistance."""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="B")
    return pd.DataFrame(rows, index=idx)


def test_no_setup_without_enough_history():
    df = _bars([{"open": 10, "high": 10.5, "low": 9.5, "close": 10, "sma_50": 10, "sma_20": 10, "resistance": 11}] * 3)
    setup = find_pullback_setup(df, i=1, config=_config())
    assert setup is None


def test_detects_pullback_in_confirmed_uptrend():
    rows = []
    # Rising sma_50 over the lookback, close above sma_50, pullback bar's
    # low dips to/through sma_20 while close stays above sma_50.
    for i in range(5):
        rows.append({"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i,
                     "sma_50": 95 + i, "sma_20": 99 + i, "resistance": 115})
    # bar 4 (last) is the pullback candidate: low touches sma_20
    rows[4] = {"open": 103, "high": 104, "low": 98.5, "close": 103.2, "sma_50": 99, "sma_20": 99.5, "resistance": 115}
    df = _bars(rows)
    setup = find_pullback_setup(df, i=4, config=_config())
    assert setup is not None
    assert setup.pullback_high == 104
    assert setup.stop_price == 98.5
    assert setup.target_price == 115


def test_rejects_when_not_actually_in_uptrend_sma_flat():
    rows = [{"open": 100, "high": 101, "low": 99.5, "close": 100, "sma_50": 100, "sma_20": 99.9, "resistance": 110} for _ in range(5)]
    # sma_50 identical across lookback -> not "rising"
    df = _bars(rows)
    setup = find_pullback_setup(df, i=4, config=_config())
    assert setup is None


def test_rejects_when_close_below_sma50_despite_low_pullback():
    rows = []
    for i in range(5):
        rows.append({"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i,
                     "sma_50": 95 + i, "sma_20": 99 + i, "resistance": 115})
    rows[4] = {"open": 96, "high": 97, "low": 90, "close": 95, "sma_50": 99, "sma_20": 96, "resistance": 115}
    df = _bars(rows)
    setup = find_pullback_setup(df, i=4, config=_config())
    assert setup is None


def test_rejects_when_no_room_for_a_target_above_pullback_high():
    rows = []
    for i in range(5):
        rows.append({"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i,
                     "sma_50": 95 + i, "sma_20": 99 + i, "resistance": 104})
    rows[4] = {"open": 103, "high": 104, "low": 98.5, "close": 103.2, "sma_50": 99, "sma_20": 99.5, "resistance": 104}
    df = _bars(rows)
    setup = find_pullback_setup(df, i=4, config=_config())
    assert setup is None  # resistance == pullback high, no room for reward


def test_confirmation_requires_next_bar_specifically_not_any_later_bar():
    from engine.swing_strategy.technical_trigger import PullbackSetup
    idx = pd.date_range("2026-01-01", periods=3, freq="B")
    df = pd.DataFrame({
        "open": [100, 100.5, 100.2],
        "high": [104, 103.9, 106],  # bar 1 doesn't confirm, bar 2 would (but is 2 bars later)
        "low": [99, 99.5, 99.8],
        "close": [103, 103.5, 105.5],
    }, index=idx)
    setup = PullbackSetup(pullback_date=idx[0], pullback_high=104, stop_price=98, target_price=115, trigger_level=104)
    result = check_confirmation(df, pullback_i=0, setup=setup)
    assert not result.confirmed
    assert "lapsed" in result.reason
    # Confirming against bar 2 directly (as if it were the very next bar)
    # does work — proving the rejection above is about *timing*, not data.
    result_next = check_confirmation(df, pullback_i=1, setup=setup)
    assert result_next.confirmed


def test_confirmation_gap_up_fills_at_open_not_trigger_level():
    from engine.swing_strategy.technical_trigger import PullbackSetup
    idx = pd.date_range("2026-01-01", periods=2, freq="B")
    df = pd.DataFrame({"open": [100, 110], "high": [104, 111], "low": [99, 109.5], "close": [103, 110.5]}, index=idx)
    setup = PullbackSetup(pullback_date=idx[0], pullback_high=104, stop_price=98, target_price=120, trigger_level=104)
    result = check_confirmation(df, pullback_i=0, setup=setup)
    assert result.confirmed
    assert result.entry_price == 110  # the gap open, not the stale 104 trigger


def test_confirmation_does_not_assume_fill_at_the_signal_bar_close():
    # The pullback bar's own close is 103 — confirmation must never use
    # that as the entry price (that would be look-ahead / an unachievable
    # same-bar fill). Entry price must come from the *next* bar only.
    from engine.swing_strategy.technical_trigger import PullbackSetup
    idx = pd.date_range("2026-01-01", periods=2, freq="B")
    df = pd.DataFrame({"open": [100, 104.5], "high": [104, 105], "low": [99, 104.2], "close": [103, 104.8]}, index=idx)
    setup = PullbackSetup(pullback_date=idx[0], pullback_high=104, stop_price=98, target_price=120, trigger_level=104)
    result = check_confirmation(df, pullback_i=0, setup=setup)
    assert result.confirmed
    assert result.entry_price != 103  # not the pullback bar's close
    assert result.entry_price == 104.5  # next bar's open, since open already cleared the trigger


def test_intraday_confirmation_matches_bar_logic_gap_case():
    from engine.swing_strategy.technical_trigger import PullbackSetup
    setup = PullbackSetup(pullback_date=pd.Timestamp("2026-01-01"), pullback_high=104,
                           stop_price=98, target_price=120, trigger_level=104)
    result = check_confirmation_intraday(setup, session_open=110, latest_price=111)
    assert result.confirmed
    assert result.entry_price == 110


def test_intraday_confirmation_not_yet_triggered():
    from engine.swing_strategy.technical_trigger import PullbackSetup
    setup = PullbackSetup(pullback_date=pd.Timestamp("2026-01-01"), pullback_high=104,
                           stop_price=98, target_price=120, trigger_level=104)
    result = check_confirmation_intraday(setup, session_open=101, latest_price=103)
    assert not result.confirmed

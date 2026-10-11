"""Shared reporting helpers used by both backtest_runner.py and
swing_backtest.py -- small, deliberately not a framework.
"""
import math


def clean_metric(value):
    """None for undefined/non-finite metrics (0 trades, zero variance,
    etc.) -- never a silent 0, and never a raw inf/NaN that breaks JSON
    (json.dumps emits the non-standard tokens Infinity/NaN for those)."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def split_into_windows(data, n_windows=4):
    """n_windows consecutive, non-overlapping slices of data (a pandas
    Series/DataFrame), oldest first. Not walk-forward retraining -- these
    strategies have fixed thresholds, nothing is fit per window -- just a
    check for whether performance holds up across several real
    sub-periods rather than one arbitrary chronological split."""
    import operator
    if isinstance(n_windows, bool):
        raise ValueError("n_windows must be an integer")
    try:
        n_windows = operator.index(n_windows)
    except TypeError as exc:
        raise ValueError("n_windows must be an integer") from exc
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    n = len(data)
    edges = [round(i * n / n_windows) for i in range(n_windows + 1)]
    return [data.iloc[edges[i]:edges[i + 1]] for i in range(n_windows)]


def validate_economics(initial_cash, fees=0.0, slippage=0.0, position_pct=None):
    for name, value in (("initial_cash", initial_cash), ("fees", fees), ("slippage", slippage)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
    if initial_cash <= 0 or not 0 <= fees < 1 or not 0 <= slippage < 1:
        raise ValueError("Require positive capital and fees/slippage in [0, 1)")
    if position_pct is not None and (not math.isfinite(float(position_pct)) or not 0 < position_pct <= 1):
        raise ValueError("position_pct must be in (0, 1]")


def validate_index(data):
    import pandas as pd
    if not isinstance(data.index, pd.DatetimeIndex) or data.index.hasnans:
        raise ValueError("Backtests require a valid DatetimeIndex")
    if not data.index.is_unique or not data.index.is_monotonic_increasing:
        raise ValueError("Backtest dates must be unique and increasing")


def validate_prices(data):
    import numpy as np
    validate_index(data)
    values = data.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Prices must be finite and positive")

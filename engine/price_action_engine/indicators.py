"""
Technical indicators computed directly from real OHLCV data using pandas-ta.
Do NOT ask the LLM to estimate these from a text description — compute them
here, then optionally hand the computed values to the LLM for narrative
interpretation (see technical_scanner.py).
"""

import pandas as pd
import pandas_ta as ta


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    df: OHLCV DataFrame with columns ['open', 'high', 'low', 'close', 'volume'],
    indexed by date, pulled from your market data API (Alpha Vantage / Twelve Data).
    """
    df["sma_50"] = ta.sma(df["close"], length=50)
    df["sma_200"] = ta.sma(df["close"], length=200)
    df["rsi_14"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"])
    df = pd.concat([df, macd], axis=1)

    bbands = ta.bbands(df["close"], length=20)
    df = pd.concat([df, bbands], axis=1)

    return df


def find_support_resistance(df: pd.DataFrame, window: int = 10) -> dict:
    """Simple swing high/low detection for support/resistance levels.
    Replace with a more sophisticated method (e.g. clustering recent
    turning points) once the basic pipeline is working end to end."""
    recent = df.tail(window * 5)
    return {
        "support": float(recent["low"].min()),
        "resistance": float(recent["high"].max()),
    }


def rolling_support_resistance(df: pd.DataFrame, window: int = 50) -> pd.DataFrame:
    """Same idea as find_support_resistance, but as a rolling series
    instead of a single latest-point read — each row's support/resistance
    uses only that row's own trailing window, so it's safe to use in a
    day-by-day backtest without look-ahead bias."""
    return pd.DataFrame({
        "support": df["low"].rolling(window, min_periods=1).min(),
        "resistance": df["high"].rolling(window, min_periods=1).max(),
    })

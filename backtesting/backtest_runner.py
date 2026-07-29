"""
Backtesting runner using vectorbt. Section 5 of the requirements doc.

Reminders baked into this structure:
- Use point-in-time historical universes to avoid survivorship bias
- Only use data that would have actually been available at each point
  in time (no look-ahead bias) — be careful with fundamentals data especially
- Run walk-forward: tune on one period, validate on a later unseen period
"""

import vectorbt as vbt
import pandas as pd


def run_backtest(price_data: pd.Series, entries: pd.Series, exits: pd.Series, initial_cash: float = 20.0):
    """
    price_data: close prices indexed by date
    entries / exits: boolean Series aligned to price_data's index,
    generated from your entry_exit signal logic run historically
    """
    portfolio = vbt.Portfolio.from_signals(
        price_data, entries, exits, init_cash=initial_cash, fees=0.0
    )
    return portfolio


def summarize(portfolio) -> dict:
    return {
        "total_return_pct": portfolio.total_return() * 100,
        "sharpe_ratio": portfolio.sharpe_ratio(),
        "max_drawdown_pct": portfolio.max_drawdown() * 100,
        "win_rate_pct": portfolio.trades.win_rate() * 100,
    }


def walk_forward_split(price_data: pd.Series, train_pct: float = 0.6):
    """Simple train/test split. For real walk-forward testing, run this
    across multiple rolling windows rather than a single split."""
    split_idx = int(len(price_data) * train_pct)
    return price_data.iloc[:split_idx], price_data.iloc[split_idx:]

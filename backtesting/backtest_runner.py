"""
Backtesting runner using vectorbt. Section 5 of the requirements doc.

What this actually backtests, and what it doesn't:

- Technical signals: computed fresh from real historical OHLCV (Twelve
  Data), using the same indicators.py functions the live system runs.
  The trend/stop-loss/target levels here are a deterministic rule-based
  proxy for what technical_scanner.py's LLM call would narrate — running
  that LLM call at every historical bar (thousands of calls per ticker)
  isn't practical or reproducible, so this substitutes the same
  underlying indicators without the LLM narrative layer.
- Valuation: the real DCF, via the actual run_dcf() LLM call — but only
  once per historical SEC filing (fundamentals change ~annually, not
  daily), gated on that filing's real `filed` date so it's never given
  data from the future. See fundamentals_data.get_financial_summary_asof.
- Sentiment: defaults to neutral (0.0). Finnhub's free tier has no
  historical news depth (confirmed empty for both 2020 and 2022 date
  ranges) — this is a data-availability limitation, not a design choice.
- Entry/exit decisions reuse the real engine.entry_exit.signals
  evaluate_entry/evaluate_exit — not reimplemented logic.
- Point-in-time universe: each ticker backtests over as much real history
  as it actually has (bounded by its own listing/filing history), not a
  forced uniform window — avoids fabricating pre-IPO data.
"""

import os

import pandas as pd
import vectorbt as vbt
from dotenv import load_dotenv

from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from engine.valuation_engine.dcf import run_dcf
from engine.valuation_engine.fundamentals_data import get_annual_filings, get_financial_summary_asof
from engine.entry_exit.signals import evaluate_entry, evaluate_exit

load_dotenv()

NEUTRAL_SENTIMENT = 0.0
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "20"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.5"))


def _technical_proxy(row: pd.Series) -> dict:
    """Deterministic stand-in for technical_scanner.py's LLM output,
    derived from the same indicators — see module docstring for why."""
    trend = "down" if row["close"] < row["sma_50"] else "up"
    return {
        "trend": trend,
        "entry_price": float(row["close"]),
        "stop_loss": float(row["support"]),
        "target_1": float(row["resistance"]),
        "probability_estimate": 55 if trend == "up" else 45,
    }


def get_point_in_time_fair_values(ticker: str, company_name: str) -> pd.Series:
    """Real DCF fair values, one per historical 10-K filing, indexed by
    that filing's actual SEC filing date (not fiscal year end)."""
    fair_values = {}
    for filing in get_annual_filings(ticker):
        filing_context = get_financial_summary_asof(ticker, as_of=filing["filed"])
        try:
            result = run_dcf(ticker, company_name=company_name, filing_context=filing_context)
            fair_values[filing["filed"]] = result["structured"].get("estimated_fair_value_per_share")
        except Exception as exc:
            print(f"Skipping DCF for {ticker} filing dated {filing['filed']}: {exc}")

    return pd.Series(fair_values, dtype="float64").sort_index()


def generate_signals(ticker: str, company_name: str, outputsize: int = 5000):
    """
    Walk the ticker's real historical price series day by day, running
    the actual evaluate_entry/evaluate_exit logic at each step.
    Returns (price_series, entries, exits) ready for vectorbt.
    """
    df = fetch_ohlcv(ticker, outputsize=outputsize)
    df = compute_indicators(df)
    df = df.join(rolling_support_resistance(df))

    fair_values = get_point_in_time_fair_values(ticker, company_name)

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)

    in_position = False
    entry_price = stop_loss = take_profit = None

    for current_date, row in df.iterrows():
        if pd.isna(row.get("sma_50")):
            continue  # not enough history yet for a trend read

        if not in_position:
            if fair_values.empty:
                continue
            as_of = fair_values[fair_values.index <= current_date.strftime("%Y-%m-%d")]
            if as_of.empty or pd.isna(as_of.iloc[-1]):
                continue

            technical_result = _technical_proxy(row)
            valuation_result = {"estimated_fair_value_per_share": as_of.iloc[-1]}
            signal = evaluate_entry(
                ticker,
                valuation_result,
                technical_result,
                NEUTRAL_SENTIMENT,
                max_position_pct=MAX_POSITION_PCT,
                account_equity=STARTING_CAPITAL,
            )
            if signal:
                entries[current_date] = True
                in_position = True
                entry_price = row["close"]
                stop_loss = technical_result["stop_loss"]
                take_profit = technical_result["target_1"]
        else:
            exit_signal = evaluate_exit(ticker, entry_price, row["close"], stop_loss, take_profit)
            if exit_signal:
                exits[current_date] = True
                in_position = False

    return df["close"], entries, exits


def run_backtest(price_data: pd.Series, entries: pd.Series, exits: pd.Series, initial_cash: float = STARTING_CAPITAL):
    """
    price_data: close prices indexed by date
    entries / exits: boolean Series aligned to price_data's index,
    generated from your entry_exit signal logic run historically
    """
    return vbt.Portfolio.from_signals(price_data, entries, exits, init_cash=initial_cash, fees=0.0, freq="D")


def summarize(portfolio) -> dict:
    return {
        "total_return_pct": portfolio.total_return() * 100,
        "sharpe_ratio": portfolio.sharpe_ratio(),
        "max_drawdown_pct": portfolio.max_drawdown() * 100,
        "win_rate_pct": portfolio.trades.win_rate() * 100,
        "num_trades": int(portfolio.trades.count()),
    }


def walk_forward_split(price_data: pd.Series, train_pct: float = 0.6):
    """Split into an earlier and later window to check whether
    performance holds up out-of-sample. Note: this strategy's thresholds
    (20% valuation discount, etc.) are fixed constants, not fit to data —
    there's no "tuning" step here, this checks consistency across time
    rather than validating a calibration."""
    split_idx = int(len(price_data) * train_pct)
    return price_data.iloc[:split_idx], price_data.iloc[split_idx:]


def run_ticker_backtest(ticker: str, company_name: str, initial_cash: float = STARTING_CAPITAL) -> dict:
    price, entries, exits = generate_signals(ticker, company_name)
    portfolio = run_backtest(price, entries, exits, initial_cash=initial_cash)

    train_price, test_price = walk_forward_split(price)
    train_portfolio = run_backtest(
        train_price, entries.loc[train_price.index], exits.loc[train_price.index], initial_cash=initial_cash
    )
    test_portfolio = run_backtest(
        test_price, entries.loc[test_price.index], exits.loc[test_price.index], initial_cash=initial_cash
    )

    return {
        "ticker": ticker,
        "date_range": (str(price.index.min().date()), str(price.index.max().date())),
        "full_period": summarize(portfolio),
        "in_sample": summarize(train_portfolio),
        "out_of_sample": summarize(test_portfolio),
    }

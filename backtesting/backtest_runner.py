"""Historical signal replay using vectorbt; not a live-execution replica.

Technical signals reuse historical indicators and entry/exit evaluators with
neutral sentiment and a deterministic technical proxy. DCF calls receive
historical filing context, require filing-sourced shares, and become available
after the filing date. Modern LLM output is not a contemporaneous forecast.

Close-derived signals execute at the next available session open. Closing
prices mark holdings when supplied by generate_signals or close_data; callers
supplying only opens receive open-marked equity. Sizing is a configurable
fraction of remaining cash with fractional shares, not stop-risk sizing.
Fees/slippage default to zero and are configurable through BACKTEST_FEES_PCT
and BACKTEST_SLIPPAGE_PCT.

The supplied static universe is not survivorship-free. Shared replay uses the
intersection of available sessions without fabricating pre-listing prices.
Signals are precomputed per ticker: rejected fills or a truncated replay start
do not regenerate the strategy's position state. Reports measure that signal
replay, not a fully execution-coupled strategy. Per-ticker windows segment the
continuous equity path and are not independent validation experiments.
"""

import os
import math

import pandas as pd
import vectorbt as vbt
from dotenv import load_dotenv
from vectorbt.portfolio.enums import SizeType

from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from engine.valuation_engine.dcf import run_dcf
from engine.valuation_engine.fundamentals_data import get_annual_filings, get_financial_summary_asof
from engine.entry_exit.signals import evaluate_entry, evaluate_exit
from engine.entry_exit.risk import Limits
from backtesting.metrics import clean_metric, split_into_windows, validate_economics, validate_prices

load_dotenv()

NEUTRAL_SENTIMENT = 0.0
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "20"))


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
        filed = filing["filed"]
        # Preserve an invalidating observation for a newer failed/unverifiable
        # filing; never silently carry an older valid DCF through that failure.
        fair_values[filed] = float("nan")
        try:
            context = get_financial_summary_asof(ticker, as_of=filed)
            if not context:
                continue
            result = run_dcf(ticker, company_name=company_name, filing_context=context)["structured"]
            value = result.get("estimated_fair_value_per_share")
            if (result.get("share_count_source") == "filing" and value is not None
                    and math.isfinite(float(value)) and float(value) > 0):
                fair_values[filed] = float(value)
        except Exception as exc:
            print(f"Skipping DCF for {ticker} filing dated {filed}: {type(exc).__name__}")
    return pd.Series(fair_values, dtype="float64").sort_index()


def fair_value_asof(fair_values, date):
    # SEC data exposes a filing DATE, not an acceptance timestamp here. Make
    # it available only on a later session, never retroactively at that open.
    matches = fair_values[pd.to_datetime(fair_values.index).normalize() < pd.Timestamp(date).normalize()]
    if matches.empty:
        return None
    value = float(matches.iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


def generate_signals(ticker: str, company_name: str, outputsize: int = 5000):
    """
    Walk the ticker's real historical price series day by day, running
    the actual evaluate_entry/evaluate_exit logic using only data through
    each bar. Returns (open_price_series, entries, exits) ready for
    vectorbt — entries/exits are shifted forward one bar so a signal
    detected off bar N's close can never fill at that same bar's price;
    the earliest it can execute is bar N+1's open.
    """
    df = fetch_ohlcv(ticker, outputsize=outputsize)
    validate_prices(df[["open", "high", "low", "close"]])
    df = compute_indicators(df)
    df = df.join(rolling_support_resistance(df))

    fair_values = get_point_in_time_fair_values(ticker, company_name)
    limits = Limits.from_env()

    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)

    in_position = False
    entry_price = stop_loss = take_profit = None
    n = len(df)

    for i in range(n):
        row = df.iloc[i]
        current_date = df.index[i]
        if pd.isna(row.get("sma_50")):
            continue  # not enough history yet for a trend read

        if not in_position:
            if fair_values.empty or i + 1 >= n:
                continue  # no next bar left to fill a fresh entry at
            value = fair_value_asof(fair_values, current_date)
            if value is None:
                continue

            technical_result = _technical_proxy(row)
            # The loader now verifies provenance before retaining any value.
            valuation_result = {"estimated_fair_value_per_share": value, "share_count_source": "filing"}
            signal = evaluate_entry(
                ticker,
                valuation_result,
                technical_result,
                NEUTRAL_SENTIMENT,
                max_position_pct=limits.max_position_pct,
                account_equity=STARTING_CAPITAL,
            )
            if signal:
                entries[current_date] = True
                in_position = True
                entry_price = float(df.iloc[i + 1]["open"])  # the real simulated fill price
                stop_loss = technical_result["stop_loss"]
                take_profit = technical_result["target_1"]
        else:
            exit_signal = evaluate_exit(ticker, entry_price, row["close"], stop_loss, take_profit)
            if exit_signal:
                exits[current_date] = True
                in_position = False

    entries = entries.shift(1, fill_value=False)
    exits = exits.shift(1, fill_value=False)
    opens = df["open"].copy()
    # Preserve separate closing marks without breaking the existing 3-Series
    # interface. Public callers can also supply close_data explicitly.
    opens.attrs["close_data"] = df["close"].copy()
    return opens, entries, exits


def run_backtest(price_data: pd.Series, entries: pd.Series, exits: pd.Series,
                  initial_cash: float = STARTING_CAPITAL, max_position_pct: float = None,
                  fees: float = None, slippage: float = None, close_data=None):
    """
    price_data: OPEN prices indexed by date -- the fill price for a signal
    detected off the PREVIOUS bar's close (see generate_signals). Passing
    a close series here would let a signal execute against the same bar
    that produced it.
    size_type=Percent caps each simulated entry at max_position_pct of
    currently available cash. This is not equity-based or stop-risk sizing.
    """
    if max_position_pct is None:
        max_position_pct = Limits.from_env().max_position_pct
    if fees is None:
        fees = float(os.getenv("BACKTEST_FEES_PCT", "0.0"))
    if slippage is None:
        slippage = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0"))
    validate_economics(initial_cash, fees, slippage, max_position_pct)
    validate_prices(price_data)
    if price_data.empty:
        raise ValueError("Backtest requires at least one price bar")
    for signal in (entries, exits):
        if not signal.index.equals(price_data.index) or signal.isna().any():
            raise ValueError("Signals must align exactly with prices and contain no missing values")
    marks = close_data if close_data is not None else price_data.attrs.get("close_data", price_data)
    marks = marks.reindex(price_data.index)
    validate_prices(marks)
    portfolio = vbt.Portfolio.from_signals(
        marks, entries, exits, price=price_data, init_cash=initial_cash,
        size=max_position_pct, size_type=SizeType.Percent,
        fees=fees, slippage=slippage, freq="D",
    )
    portfolio._phase1_costs = (fees, slippage)
    return portfolio


def summarize(portfolio, *, fees=None, slippage=None) -> dict:
    actual_fees, actual_slippage = getattr(portfolio, "_phase1_costs", (None, None))
    fees = actual_fees if fees is None else fees
    slippage = actual_slippage if slippage is None else slippage
    closed = portfolio.trades.closed
    num_closed = int(closed.count())
    # Include the pre-trade capital peak: vectorbt's default drawdown starts
    # at the first marked bar and otherwise hides a first-bar entry cost.
    values = portfolio.value()
    peak = float(portfolio.get_init_cash())
    drawdown = 0.0
    for value in values:
        peak = max(peak, float(value))
        drawdown = max(drawdown, (peak-float(value))/peak if peak > 0 else 0.0)
    return {
        "total_return_pct": clean_metric(portfolio.total_return() * 100),
        "sharpe_ratio": clean_metric(portfolio.returns().vbt.returns(freq="1D", year_freq="252D").sharpe_ratio()),
        "max_drawdown_pct": clean_metric(drawdown * 100),
        "win_rate_pct": clean_metric(closed.win_rate() * 100) if num_closed else None,
        "num_trades": int(portfolio.trades.count()),
        "num_closed_trades": num_closed,
        "num_open_trades": int(portfolio.trades.open.count()),
        "fees_pct": fees, "slippage_pct": slippage,
        "annualization_sessions": 252,
        "sizing_basis": "fraction_of_remaining_cash",  # not live equity/risk parity
    }


def summarize_ticker_backtest(ticker: str, price: pd.Series, entries: pd.Series, exits: pd.Series,
                               initial_cash: float = STARTING_CAPITAL, n_windows: int = 4) -> dict:
    """Builds the full-period + windowed report from already-generated
    signals -- split out from run_ticker_backtest so a caller that needs
    the raw (price, entries, exits) for its own purposes too (see
    run_shared_capital_backtest below) doesn't re-fetch OHLCV or re-run
    real DCF LLM calls a second time for the same ticker."""
    limits = Limits.from_env()
    fees = float(os.getenv("BACKTEST_FEES_PCT", "0.0"))
    slippage = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0"))

    portfolio = run_backtest(price, entries, exits, initial_cash=initial_cash,
                             max_position_pct=limits.max_position_pct, fees=fees, slippage=slippage)
    full_period = summarize(portfolio)
    equity = portfolio.value()
    windows = []
    preceding_equity = initial_cash
    for w_equity in split_into_windows(equity, n_windows=n_windows):
        if w_equity.empty:
            continue
        curve = [preceding_equity] + w_equity.tolist()
        peak, dd = curve[0], 0.0
        for value in curve:
            peak = max(peak, value)
            dd = max(dd, (peak - value) / peak if peak > 0 else 0)
        windows.append({
            "date_range": (str(w_equity.index.min().date()), str(w_equity.index.max().date())),
            "window_type": "continuous_portfolio_equity_segment",
            "starting_equity": float(preceding_equity), "ending_equity": float(w_equity.iloc[-1]),
            "total_return_pct": clean_metric((w_equity.iloc[-1] / preceding_equity - 1) * 100) if preceding_equity > 0 else None,
            "max_drawdown_pct": clean_metric(dd * 100),
        })
        preceding_equity = float(w_equity.iloc[-1])

    return {
        "ticker": ticker,
        "date_range": (str(price.index.min().date()), str(price.index.max().date())),
        "full_period": full_period,
        # Several non-overlapping real sub-periods, not walk-forward
        # retraining -- there's no fitted parameter here to refit per
        # window (see module docstring). Checks consistency over time.
        "windows": windows,
    }


def run_ticker_backtest(ticker: str, company_name: str, initial_cash: float = STARTING_CAPITAL,
                         n_windows: int = 4) -> dict:
    """Independent, isolated-capital backtest for one ticker -- useful
    per-ticker diagnostic, but NOT a portfolio stat: each call like this
    gets its own fresh init_cash, so two tickers run this way never
    compete for the same dollars. See run_shared_capital_backtest for the
    real aggregate-portfolio figure."""
    price, entries, exits = generate_signals(ticker, company_name)
    return summarize_ticker_backtest(ticker, price, entries, exits, initial_cash=initial_cash, n_windows=n_windows)


def run_shared_capital_backtest(signals_by_ticker: dict, initial_cash: float = STARTING_CAPITAL) -> dict:
    """One real shared-capital portfolio across every ticker in
    signals_by_ticker ({ticker: (price, entries, exits)} from
    generate_signals) -- vectorbt's own cash_sharing=True/group_by
    mechanism, not a hand-rolled simulator. Two tickers both wanting to
    enter now genuinely compete for the same dollars, unlike the
    independent per-ticker runs above.

    Restricted to the intersection of dates where every included ticker
    already has data, so a ticker that listed later never gets fabricated
    pre-IPO history. The supplied universe may still have survivorship bias.
    Precomputed signals retain their original per-ticker position state;
    clipping dates does not independently restart signal generation.
    """
    if not signals_by_ticker:
        return {"supported": False, "reason": "no tickers with usable signals"}

    tickers = sorted(signals_by_ticker)
    common_index = None
    for price, entries, exits in signals_by_ticker.values():
        validate_prices(price)
        if not entries.index.equals(price.index) or not exits.index.equals(price.index) or entries.isna().any() or exits.isna().any():
            raise ValueError("Signals must align exactly with prices")
        common_index = price.index if common_index is None else common_index.intersection(price.index)
    if common_index is None or len(common_index) < 2:
        return {"supported": False, "reason": "no overlapping date range across the given tickers"}

    price_df = pd.DataFrame({t: signals_by_ticker[t][0].reindex(common_index) for t in tickers})
    entries_df = pd.DataFrame({t: signals_by_ticker[t][1].reindex(common_index, fill_value=False) for t in tickers})
    exits_df = pd.DataFrame({t: signals_by_ticker[t][2].reindex(common_index, fill_value=False) for t in tickers})

    limits = Limits.from_env()
    fees = float(os.getenv("BACKTEST_FEES_PCT", "0.0"))
    slippage = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0"))

    validate_economics(initial_cash, fees, slippage, limits.max_position_pct)
    marks = pd.DataFrame({t: signals_by_ticker[t][0].attrs.get("close_data", signals_by_ticker[t][0]).reindex(common_index) for t in tickers})
    validate_prices(marks)
    portfolio = vbt.Portfolio.from_signals(
        marks, entries_df, exits_df, price=price_df, init_cash=initial_cash,
        size=limits.max_position_pct, size_type=SizeType.Percent,
        fees=fees, slippage=slippage, freq="D",
        cash_sharing=True, group_by=True,
    )
    return {
        "supported": True,
        "tickers": tickers,
        "execution_order": "alphabetical; remaining-cash allocation",
        "window_policy": "intersection of available sessions; starts flat",
        "signal_policy": "precomputed per-ticker replay; state is not regenerated after clipping or rejected fills",
        "date_range": (str(common_index.min().date()), str(common_index.max().date())),
        **summarize(portfolio, fees=fees, slippage=slippage),
    }

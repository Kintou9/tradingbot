"""
Driver script: runs the swing-strategy variant comparison across the real
watchlist and prints a report. Mirrors run_watchlist_backtest.py's own
"one-off driver, not part of the production pipeline" role.

Usage:
    python -m backtesting.run_swing_backtest              # variant A only, full watchlist (free, fast)
    python -m backtesting.run_swing_backtest --with-dcf    # also runs B and D (real LLM cost — see warning below)
    python -m backtesting.run_swing_backtest --tickers MSFT AVPT

Variants C and D always report as unsupported for this historical
comparison — see backtesting/swing_backtest.py's module docstring for why
(no historical news-sentiment depth available). They ARE evaluated
prospectively by functions/swing_paper_trading.py --variant C/D going
forward; this script cannot back-fill that.

Twelve Data's free tier rate-limits at roughly 8 requests/minute — this
script paces OHLCV fetches accordingly, so a full 10-ticker run takes a
couple of minutes even for variant A alone.
"""

import argparse
import json
import time

from functions.market_hours_trading import WATCHLIST
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.valuation_engine.fundamentals_data import get_company_name
from backtesting.swing_backtest import run_swing_backtest_for_ticker, _summarize

OHLCV_CALL_PACING_SECONDS = 9.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="*", default=None, help="override the default watchlist")
    parser.add_argument("--with-dcf", action="store_true", help="also run variants B and D (real Anthropic API cost — roughly one call per historical 10-K filing per ticker)")
    args = parser.parse_args()

    tickers = args.tickers or WATCHLIST
    variants = [GateVariant.TECHNICAL_ONLY] + ([GateVariant.TECHNICAL_PLUS_DCF] if args.with_dcf else [])

    report = {}
    for variant in variants:
        config = SwingStrategyConfig(gate_variant=variant)
        needs_dcf = variant == GateVariant.TECHNICAL_PLUS_DCF
        per_ticker = {}
        pooled_trades = []
        for i, ticker in enumerate(tickers):
            if i > 0:
                time.sleep(OHLCV_CALL_PACING_SECONDS)
            company_name = get_company_name(ticker) if needs_dcf else None
            try:
                result = run_swing_backtest_for_ticker(ticker, company_name, config)
                if not result.supported:
                    per_ticker[ticker] = {"supported": False, "reason": result.reason}
                    continue
                per_ticker[ticker] = {"candidates": result.candidates_evaluated, **_summarize(result.trades, 100_000)}
                pooled_trades.extend(result.trades)
            except Exception as exc:
                per_ticker[ticker] = {"error": str(exc)}
        report[variant.value] = {"per_ticker": per_ticker, "pooled": _summarize(pooled_trades, 100_000)}

    for v in ("C", "D"):
        if v == "D" and not args.with_dcf:
            continue
        from backtesting.swing_backtest import HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE
        report[v] = {"supported": False, "reason": HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE}

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

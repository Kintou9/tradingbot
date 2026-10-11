"""
Driver script: runs the swing-strategy variant comparison across the real
watchlist and prints a report. Mirrors run_watchlist_backtest.py's own
"one-off driver, not part of the production pipeline" role.

Usage:
    python -m backtesting.run_swing_backtest              # variant A only, full watchlist (free, fast)
    python -m backtesting.run_swing_backtest --with-dcf    # also runs B (real LLM cost — see warning below)
    python -m backtesting.run_swing_backtest --tickers MSFT AVPT

Variants C and D always report as unsupported for this historical
comparison — see backtesting/swing_backtest.py's module docstring for why
(no historical news-sentiment depth available). They ARE evaluated
prospectively by functions/swing_paper_trading.py --variant C/D going
forward; this script cannot back-fill that.

Reports BOTH an independent per-ticker view and a real shared-capital
portfolio view per variant (see backtesting/swing_backtest.py's module
docstring) — not just a pooled sum of isolated runs.

Twelve Data's free tier rate-limits at roughly 8 requests/minute — this
script paces OHLCV fetches accordingly, so a full 10-ticker run takes a
couple of minutes even for variant A alone.
"""

import argparse
import json

from functions.market_hours_trading import WATCHLIST
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.valuation_engine.fundamentals_data import get_company_name
from backtesting.swing_backtest import compare_variants

OHLCV_CALL_PACING_SECONDS = 9.0
ACCOUNT_EQUITY = 100_000.0


def _run_variant(variant: GateVariant, tickers: list) -> dict:
    names = [(t, get_company_name(t) if variant == GateVariant.TECHNICAL_PLUS_DCF else None) for t in tickers]
    return compare_variants(names, SwingStrategyConfig(), account_equity=ACCOUNT_EQUITY,
        variants=[variant], fetch_pause_seconds=OHLCV_CALL_PACING_SECONDS)[variant.value]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="*", default=None, help="override the default watchlist")
    parser.add_argument("--with-dcf", action="store_true", help="also run variant B (real Anthropic API cost — roughly one call per historical 10-K filing per ticker)")
    args = parser.parse_args()

    tickers = args.tickers or WATCHLIST
    variants = [GateVariant.TECHNICAL_ONLY] + ([GateVariant.TECHNICAL_PLUS_DCF] if args.with_dcf else [])

    # One call shares the input snapshot across A/B instead of refetching
    # each strategy's prices and silently comparing different cohorts.
    names = []
    for ticker in tickers:
        try:
            name = get_company_name(ticker) if args.with_dcf else None
        except Exception:
            name = None
        names.append((ticker, name))
    report = compare_variants(names, SwingStrategyConfig(), account_equity=ACCOUNT_EQUITY,
        variants=variants + [GateVariant.TECHNICAL_PLUS_SENTIMENT, GateVariant.TECHNICAL_PLUS_BOTH],
        fetch_pause_seconds=OHLCV_CALL_PACING_SECONDS)
    print(json.dumps(report, indent=2, allow_nan=False))



if __name__ == "__main__":
    main()

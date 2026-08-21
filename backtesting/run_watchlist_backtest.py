"""One-off driver: run_ticker_backtest across the real watchlist and print
a results table. Not part of the production pipeline — a script for
generating a backtest report on demand."""

import json

from functions.after_hours_research import WATCHLIST
from backtesting.backtest_runner import run_ticker_backtest
from engine.valuation_engine.fundamentals_data import get_company_name

results = []
for ticker in WATCHLIST:
    print(f"=== {ticker} ===", flush=True)
    try:
        company_name = get_company_name(ticker)
        if company_name is None:
            print(f"Skipping {ticker}: no SEC CIK found")
            continue
        result = run_ticker_backtest(ticker, company_name)
        results.append(result)
        print(json.dumps(result, indent=2, default=str), flush=True)
    except Exception as exc:
        print(f"Skipping {ticker}: {exc}", flush=True)

print("\n=== ALL RESULTS ===")
print(json.dumps(results, indent=2, default=str))

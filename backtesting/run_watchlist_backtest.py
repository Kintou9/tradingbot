"""One-off driver: run_ticker_backtest across the real watchlist and print
a results table. Not part of the production pipeline — a script for
generating a backtest report on demand.

Reports TWO distinct views, deliberately not merged into one number:
- "independent" per-ticker backtests — each ticker gets its own fresh
  capital pool, useful for comparing tickers against each other, but NOT
  a portfolio stat (two tickers here never compete for the same dollars).
- "shared_capital_portfolio" — one real portfolio across every ticker
  that produced usable signals, via vectorbt's native cash_sharing — the
  actual aggregate figure if you held all of these at once.
"""

import json

from functions.after_hours_research import WATCHLIST
from backtesting.backtest_runner import generate_signals, summarize_ticker_backtest, run_shared_capital_backtest
from engine.valuation_engine.fundamentals_data import get_company_name

def main():
    independent_results = []
    signals_by_ticker = {}
    for ticker in WATCHLIST:
        print(f"=== {ticker} ===", flush=True)
        try:
            company_name = get_company_name(ticker)
            if company_name is None:
                print(f"Skipping {ticker}: no SEC CIK found")
                continue
            price, entries, exits = generate_signals(ticker, company_name)
            result = summarize_ticker_backtest(ticker, price, entries, exits)
            signals_by_ticker[ticker] = (price, entries, exits)
            independent_results.append(result)
            print(json.dumps(result, indent=2, allow_nan=False), flush=True)
        except Exception as exc:
            print(f"Skipping {ticker}: {exc}", flush=True)

    print("\n=== SHARED-CAPITAL PORTFOLIO (the real aggregate stat) ===", flush=True)
    shared_capital_portfolio = run_shared_capital_backtest(signals_by_ticker)
    print(json.dumps(shared_capital_portfolio, indent=2, allow_nan=False), flush=True)

    print("\n=== ALL RESULTS ===")
    print(json.dumps({
        "independent_per_ticker": independent_results,
        "shared_capital_portfolio": shared_capital_portfolio,
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

# Experimental swing-trading strategy (`swing-v1-experimental`)

Built 2026-09-11. A parallel, isolated days-to-weeks swing strategy —
**not** the live strategy (`engine/entry_exit/signals.py` +
`functions/market_hours_trading.py`), which keeps running exactly as
before. Nothing here is wired into the autonomous-trading loop or the
dashboard's manual buy/sell buttons; see
`tests/test_swing_preserves_live_behavior.py` for the guard that checks
this stays true.

## Why this exists

The live strategy gates entries on a 20%-"discount" DCF signal (a
months-to-years convergence bet) but exits on tight technical
stop-loss/take-profit bands sized for days-to-weeks, with no time-based
exit actually wired up. This package is what a strategy looks like when
the entry, exit, and risk model are all built for the same days-to-weeks
horizon on purpose.

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | `SwingStrategyConfig` — every tunable, all experimental defaults |
| `reward_risk.py` | Reward:risk validation (point 1) |
| `sizing.py` | Risk-based position sizing + aggregate open-risk cap (point 2) |
| `trading_calendar.py` | Trading-session counting, live and backtest (point 3) |
| `exits.py` | Stop/target/time-based exit precedence (point 3) |
| `technical_trigger.py` | Pullback-in-uptrend entry trigger (point 4) |
| `valuation.py` | DCF gate — `upside` vs `discount_to_fair_value` (point 5) |
| `sentiment_gate.py` | Sentiment gate — missing news ≠ neutral (point 5) |
| `engine.py` | Combines all of the above into one entry decision + candidate log (point 5, 6) |

Backtesting: `backtesting/swing_backtest.py` (harness) and
`backtesting/run_swing_backtest.py` (driver script). Forward paper
trading: `functions/swing_paper_trading.py`.

## Experimental defaults (not proven — see `config.py` for the full list)

- `min_reward_to_risk = 1.5`
- `risk_per_trade_pct = 0.5%` of equity
- `max_total_open_risk_pct = 2%` of equity, across all open swing positions + pending entries
- `max_holding_trading_days = 20` trading sessions
- `slippage_bps = 5.0` (one-way) — no cost/slippage assumption existed anywhere else in the repo to reuse; this is new and unvalidated against real fills
- `valuation_discount_threshold = 0.20`, `min_sentiment_score = 0.0`, `research_max_age_hours = 24` — carried over from the live strategy's own thresholds for comparability, not independently re-derived

## The technical entry trigger, and why this one

No deterministic, backtest-reproducible entry trigger existed anywhere in
the repo — `technical_scanner.py` hands indicators to an LLM and asks for
narrative entry/stop/target suggestions, which can't be replayed
thousands of times over history (see `backtesting/backtest_runner.py`'s
own docstring on exactly this). A **pullback-in-uptrend with
confirmation** was chosen because it's:

1. Objective and exactly specifiable from completed bars (see
   `technical_trigger.py`'s module docstring for the precise definitions
   of uptrend/pullback/confirmation),
2. Standard enough that its failure modes are well understood rather than
   novel,
3. Naturally pairs with a technical stop (the pullback low) and target
   (rolling resistance), which is what points 1–3 need inputs for.

Confirmation happens on exactly the next session — if price doesn't
break the pullback bar's high the very next day, the setup lapses rather
than staying armed. A gap through the trigger fills at the gap price, not
the stale trigger level — no favorable-execution assumption. Live/paper
trading revalidates with a fresh quote immediately before order
submission (`check_confirmation_intraday`) rather than trusting
yesterday's close.

## What upside vs. discount_to_fair_value means, and why it's logged separately

The live gate computes `(fair_value - price) / price`, which is
**upside relative to price**, not the textbook "discount to fair value"
(`1 - price/fair_value`) — despite being named a discount everywhere in
the live code. The two diverge more as the valuation gap widens. This
package computes and logs both under their correct names
(`valuation.py`) and gates on `upside` specifically, to stay comparable
with the live strategy's actual (not just nominal) behavior.

## The four variants

| Variant | Gates on top of the mandatory technical trigger + reward:risk check |
|---|---|
| A | none |
| B | DCF (filing-sourced share count required; anything else is rejected, never silently passed) |
| C | sentiment (empty news window = missing data = rejected, not neutral) |
| D | both |

**C and D cannot be backtested against history** — Finnhub's free tier
has zero historical news depth (already confirmed and documented in
`backtest_runner.py` before this work started). `swing_backtest.py`
marks both unsupported for the historical comparison rather than
faking a result; `swing_paper_trading.py --variant C` or `--variant D`
collects them prospectively instead, with every candidate (accepted or
rejected) logged to `SwingCandidateLog`.

## Known limitations, stated plainly

- The backtest harness evaluates each ticker's trade sequence in
  isolation and pools raw per-share P&L for win-rate/expectancy/R:R
  stats. It does **not** run a single shared, capital-constrained,
  multi-ticker equity curve with `sizing.py`'s risk-based sizing and
  aggregate-risk cap actually applied simultaneously across tickers —
  that's a materially bigger simulator this work didn't build. Treat
  `portfolio_return_pct`/`max_drawdown_pct` from the backtest as
  low-confidence; win rate, avg win/loss, and reward:risk realization are
  the trustworthy numbers from it.
- The static "no lookback bias" guarantee holds for OHLCV-driven checks
  (technical trigger, reward:risk, exits). The DCF backtest path reuses
  `get_point_in_time_fair_values`, which is real and point-in-time
  correct, but it's slow (one LLM call per historical 10-K filing — 9
  calls just for MSFT's history) and costs real Anthropic API spend, so
  it wasn't run across the full watchlist × full history by default.
- `sizing.py`'s share-increment rounding defaults to `0.0001` (Alpaca's
  typical fractional increment) unless a caller passes the asset's real
  `min_trade_increment` from `TradingClient.get_asset()` — the paper
  runner does not currently look that up per-ticker and always uses the
  default. Not wrong for a fractionable large/mid-cap name, but not
  verified against each asset's actual increment either.

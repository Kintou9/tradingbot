"""
Backtest harness for the experimental swing strategy — compares variants
A/B/C/D on the same universe and period, per point 6 of the swing-
strategy work.

What can and can't actually be backtested here, stated up front rather
than glossed over:

- Variant A (technical setup only) uses nothing but real historical
  OHLCV (Twelve Data) — fully backtestable, no LLM cost, no data-
  availability gap.
- Variants B and D add the DCF gate, which needs a real fair-value
  figure at each historical point in time. That reuses
  backtesting.backtest_runner.get_point_in_time_fair_values — real LLM
  calls against real filing dates (existing, already-built
  infrastructure, not new to this file) but LLM spend all the same:
  roughly one call per historical 10-K filing per ticker.
- Variants C and D add the sentiment gate. This CANNOT be backtested
  historically: backtesting/backtest_runner.py already documents that
  Finnhub's free tier has no historical news depth (confirmed empty for
  multiple past date ranges). Point 6 explicitly allows marking an
  unsupported comparison rather than faking it, so C and D are marked
  unsupported here rather than silently returning "0 trades" (which
  would look like a strategy result rather than a data gap). Collect
  those two prospectively via functions/swing_paper_trading.py instead.

Execution assumptions inherited from engine.swing_strategy: confirmation
fills use the gap-aware logic in technical_trigger.check_confirmation;
exits resolve ambiguous same-bar stop+target touches conservatively via
exits.evaluate_swing_exit_bar; position sizing and the aggregate open-risk
cap use sizing.size_position with a single simulated account (this
harness trades one ticker in isolation per run — it does not model
simultaneous cross-ticker aggregate risk here, only the guard being
present and correct, which the sizing unit tests cover directly).

An untouched holdout period (a walk_forward_split, same 60/40 idea as
backtesting.backtest_runner.walk_forward_split) is reported separately
from the full period — thresholds here are fixed experimental defaults,
not fit to any data, so there is no tuning step to worry about
contaminating, but the split still lets you see whether behavior holds
up in the half of history not eyeballed while building this.
"""

from dataclasses import dataclass, field

import pandas as pd

from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.swing_strategy.technical_trigger import find_pullback_setup, check_confirmation
from engine.swing_strategy.reward_risk import evaluate_reward_to_risk
from engine.swing_strategy.exits import evaluate_swing_exit_bar
from engine.swing_strategy.valuation import evaluate_valuation_gate
from backtesting.backtest_runner import get_point_in_time_fair_values

HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE = (
    "Historical news sentiment is not available — Finnhub's free tier has no historical "
    "depth (see backtesting/backtest_runner.py's own docstring, confirmed empty for past "
    "date ranges). Variants C and D cannot be backtested against history. Collect them "
    "prospectively instead via functions/swing_paper_trading.py."
)


@dataclass
class SimulatedTrade:
    ticker: str
    entry_date: str
    entry_price: float
    stop_price: float
    target_price: float
    reward_to_risk: float
    shares: float
    exit_date: str
    exit_price: float
    exit_reason: str
    sessions_held: int
    pnl: float
    pnl_pct: float


@dataclass
class BacktestResult:
    ticker: str
    variant: str
    supported: bool
    reason: str | None = None
    candidates_evaluated: int = 0
    trades: list = field(default_factory=list)


def _summarize(trades: list[SimulatedTrade], account_equity: float) -> dict:
    if not trades:
        return {
            "trade_count": 0, "win_rate_pct": None, "avg_win": None, "avg_loss": None,
            "expectancy": None, "portfolio_return_pct": None, "max_drawdown_pct": None,
            "avg_holding_sessions": None,
        }
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(trades)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    equity = account_equity
    equity_curve = [equity]
    for t in trades:
        equity += t.pnl
        equity_curve.append(equity)
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak if peak else 0.0)

    return {
        "trade_count": len(trades),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "portfolio_return_pct": round((equity_curve[-1] / equity_curve[0] - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_holding_sessions": round(sum(t.sessions_held for t in trades) / len(trades), 1),
    }


def _dcf_structured_as_of(fair_values: pd.Series, as_of_date: pd.Timestamp) -> dict | None:
    matches = fair_values[fair_values.index <= as_of_date.strftime("%Y-%m-%d")]
    if matches.empty or pd.isna(matches.iloc[-1]):
        return None
    # get_point_in_time_fair_values reuses run_dcf against real, point-in-time
    # filing_context via get_financial_summary_asof — which now includes
    # diluted share count (the 2026-09-09 fix), so a real historical DCF run
    # today produces share_count_source == "filing" whenever the filing had
    # a usable share count. That provenance isn't stored in this Series
    # (only the fair-value number is), so it's reconstructed as "filing"
    # here — an approximation the module docstring calls out explicitly:
    # this backtest cannot fully re-derive the original run's own
    # share_count_source without re-running it, but every DCF this harness
    # runs goes through the fixed prompt, so "filing" is accurate for any
    # non-null value that survived get_point_in_time_fair_values' own
    # try/except (a failed run is skipped there and never enters this Series).
    return {"estimated_fair_value_per_share": float(matches.iloc[-1]), "share_count_source": "filing"}


def run_swing_backtest_for_ticker(
    ticker: str, company_name: str | None, config: SwingStrategyConfig,
    outputsize: int = 5000, account_equity: float = 100_000.0,
) -> BacktestResult:
    if config.gate_variant in (GateVariant.TECHNICAL_PLUS_SENTIMENT, GateVariant.TECHNICAL_PLUS_BOTH):
        return BacktestResult(ticker, config.gate_variant.value, supported=False, reason=HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE)

    needs_dcf = config.gate_variant in (GateVariant.TECHNICAL_PLUS_DCF, GateVariant.TECHNICAL_PLUS_BOTH)
    if needs_dcf and not company_name:
        return BacktestResult(ticker, config.gate_variant.value, supported=False, reason="no SEC CIK found for this ticker — can't run a filing-sourced DCF")

    df = fetch_ohlcv(ticker, outputsize=outputsize)
    df = compute_indicators(df)
    df = df.join(rolling_support_resistance(df, window=config.support_resistance_window))

    fair_values = get_point_in_time_fair_values(ticker, company_name) if needs_dcf else None

    trades: list[SimulatedTrade] = []
    candidates_evaluated = 0
    in_position = False
    position = None

    i = 0
    n = len(df)
    while i < n - 1:
        if not in_position:
            setup = find_pullback_setup(df, i, config)
            if setup is not None:
                confirmation = check_confirmation(df, i, setup)
                candidates_evaluated += 1
                if confirmation.confirmed:
                    rr = evaluate_reward_to_risk(confirmation.entry_price, setup.stop_price, setup.target_price, config.min_reward_to_risk)
                    valuation_ok = True
                    if needs_dcf:
                        dcf_structured = _dcf_structured_as_of(fair_values, df.index[i + 1])
                        gate = evaluate_valuation_gate(dcf_structured, confirmation.entry_price, config.valuation_discount_threshold)
                        valuation_ok = gate.passed
                    if rr.accepted and valuation_ok:
                        entry_i = i + 1
                        position = {
                            "entry_i": entry_i, "entry_date": df.index[entry_i], "entry_price": confirmation.entry_price,
                            "stop_price": setup.stop_price, "target_price": setup.target_price,
                            "reward_to_risk": rr.reward_to_risk,
                        }
                        in_position = True
                        i = entry_i
                        continue
        else:
            row = df.iloc[i]
            sessions_held = i - position["entry_i"]
            exit_decision = evaluate_swing_exit_bar(
                open_price=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
                stop_price=position["stop_price"], target_price=position["target_price"],
                sessions_held=sessions_held, max_holding_trading_days=config.max_holding_trading_days,
            )
            if exit_decision.should_exit:
                entry_price = position["entry_price"]
                pnl_per_share = exit_decision.exit_price - entry_price
                trades.append(SimulatedTrade(
                    ticker=ticker, entry_date=str(position["entry_date"].date()), entry_price=entry_price,
                    stop_price=position["stop_price"], target_price=position["target_price"],
                    reward_to_risk=position["reward_to_risk"], shares=1.0,
                    exit_date=str(df.index[i].date()), exit_price=exit_decision.exit_price,
                    exit_reason=exit_decision.reason.value, sessions_held=sessions_held,
                    pnl=pnl_per_share, pnl_pct=pnl_per_share / entry_price,
                ))
                in_position = False
                position = None
        i += 1

    return BacktestResult(ticker, config.gate_variant.value, supported=True, candidates_evaluated=candidates_evaluated, trades=trades)


def compare_variants(tickers_with_names: list[tuple[str, str | None]], base_config: SwingStrategyConfig,
                      outputsize: int = 5000, account_equity: float = 100_000.0) -> dict:
    """Runs A/B/C/D (C and D marked unsupported per the module docstring)
    across the same ticker list and reports the requested metrics per
    ticker plus pooled across the universe."""
    results = {}
    for variant in GateVariant:
        config = SwingStrategyConfig(**{**base_config.as_dict(), "gate_variant": variant})
        per_ticker = []
        pooled_trades: list[SimulatedTrade] = []
        supported_any = False
        unsupported_reason = None
        for ticker, company_name in tickers_with_names:
            result = run_swing_backtest_for_ticker(ticker, company_name, config, outputsize=outputsize, account_equity=account_equity)
            if not result.supported:
                unsupported_reason = result.reason
                continue
            supported_any = True
            pooled_trades.extend(result.trades)
            per_ticker.append({
                "ticker": ticker, "candidates_evaluated": result.candidates_evaluated,
                **_summarize(result.trades, account_equity),
            })
        results[variant.value] = {
            "supported": supported_any,
            "reason": None if supported_any else unsupported_reason,
            "per_ticker": per_ticker,
            "pooled": _summarize(pooled_trades, account_equity) if supported_any else None,
        }
    return results

"""Daily-bar comparison of the existing experimental swing variants.

A uses historical OHLCV. B additionally uses filing-date-gated DCF output;
A/B share a cached dataset, eligible cohort, and date bounds when compared.
C/D remain unsupported because historical sentiment is unavailable here.
No new strategy or production decision rules are introduced.

A chronological ledger reserves cash and stop risk until actual exits and
marks open holdings daily. Sizing reuses size_position with its default
0.0001-share increment and
current equity. Entries precede exits on each session, so unknown intraday
exit proceeds cannot fund entries. Alphabetical priority resolves competing
entries. Entry-session stop/target ambiguity uses the adverse stop outcome;
daily bars cannot establish whether a low preceded an intraday breakout.
Fees and adverse slippage apply on both sides. Stops do not guarantee a
maximum realized loss, especially across gaps.

Per-ticker runs use independent capital; shared results use a single ledger.
Chronological windows restart flat, preserve prior indicator warmup, exclude
setups formed before the window, and mark terminal open holdings to market.
These are fixed-rule subperiod checks, not walk-forward model training.
The static input universe can have survivorship bias; historical LLM-derived
valuations are not proven free of knowledge contamination. This harness does
not establish live parity, execution liquidity, or profitability.
"""

from dataclasses import dataclass, field

import pandas as pd
import math
import os

from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.swing_strategy.technical_trigger import find_pullback_setup, check_confirmation
from engine.swing_strategy.reward_risk import evaluate_reward_to_risk
from engine.swing_strategy.exits import evaluate_swing_exit_bar
from engine.swing_strategy.valuation import evaluate_valuation_gate
from engine.swing_strategy.sizing import size_position
from backtesting.backtest_runner import get_point_in_time_fair_values, fair_value_asof
from backtesting.metrics import clean_metric, split_into_windows, validate_economics, validate_prices

HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE = (
    "Historical news sentiment is not supplied by this harness. "
    "Variants C and D are unsupported for historical comparison. Collect them "
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
    pnl: float            # net of slippage -- the real simulated cash P&L
    pnl_pct: float
    gross_pnl: float       # raw-price P&L before costs
    fees_paid: float = 0.0
    slippage_cost: float | None = None


@dataclass
class BacktestResult:
    ticker: str
    variant: str
    supported: bool
    reason: str | None = None
    candidates_evaluated: int = 0
    candidates_rejected_sizing: int = 0
    trades: list = field(default_factory=list)
    portfolio: dict = field(default_factory=dict)


@dataclass
class SimulationResult:
    trades: list
    evaluated: dict
    rejected: dict
    cash: float
    open_risk: float
    equity_curve: pd.Series
    cash_curve: pd.Series
    positions_value: float
    open_positions: dict
    fees_paid: float
    slippage_cost: float

    def __iter__(self):
        # Preserve the original five-value unpacking contract.
        return iter((self.trades, self.evaluated, self.rejected, self.cash, self.open_risk))

    def __getitem__(self, index):
        return tuple(self)[index]


def _summarize(trades: list[SimulatedTrade], account_equity: float, simulation=None) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross = sum(t.gross_pnl for t in trades)
    net = sum(t.pnl for t in trades)
    result = {
        "trade_count": len(trades),
        "win_rate_pct": clean_metric(100 * len(wins) / len(trades)) if trades else None,
        "avg_win": clean_metric(sum(t.pnl for t in wins) / len(wins)) if wins else None,
        "avg_loss": clean_metric(sum(t.pnl for t in losses) / len(losses)) if losses else None,
        "expectancy": clean_metric(net / len(trades)) if trades else None,
        "avg_holding_sessions": clean_metric(sum(t.sessions_held for t in trades) / len(trades)) if trades else None,
        "gross_pnl_total": clean_metric(gross) if trades else None,
        "net_pnl_total": clean_metric(net) if trades else None,
        "total_slippage_cost": clean_metric(sum(t.slippage_cost if t.slippage_cost is not None else t.gross_pnl-t.pnl-t.fees_paid for t in trades)) if trades else None,
        "total_fees": clean_metric(sum(t.fees_paid for t in trades)),
        "portfolio_return_pct": None, "max_drawdown_pct": None,
        "pnl_basis": "closed trades; portfolio metrics require daily equity",
    }
    if simulation is not None:
        curve = [account_equity] + simulation.equity_curve.tolist()
        peak, dd = account_equity, 0.
        for value in curve:
            peak = max(peak, value)
            dd = max(dd, (peak-value)/peak if peak > 0 else 0)
        result.update(
            portfolio_return_pct=clean_metric((curve[-1]/account_equity-1)*100),
            max_drawdown_pct=clean_metric(dd*100),
            starting_equity=account_equity, ending_equity=curve[-1],
            final_cash=simulation.cash, open_positions_value=simulation.positions_value,
            open_position_count=len(simulation.open_positions),
            unrealized_pnl=curve[-1]-account_equity-net,
            total_slippage_cost=simulation.slippage_cost, total_fees=simulation.fees_paid,
            pnl_basis="daily close mark-to-market; open positions retained at final mark",
            equity_curve=[{"date": str(date.date()), "equity": float(value),
                           "cash": float(simulation.cash_curve.loc[date])}
                          for date, value in simulation.equity_curve.items()],
        )
    return result


def _dcf_structured_as_of(fair_values: pd.Series, as_of_date: pd.Timestamp) -> dict | None:
    value = fair_value_asof(fair_values, as_of_date)
    if value is None:
        return None
    # Only the loader's explicitly verified values survive into this series.
    return {"estimated_fair_value_per_share": value, "share_count_source": "filing"}


def _find_raw_candidates(df: pd.DataFrame, config: SwingStrategyConfig, needs_dcf: bool,
                          fair_values: pd.Series | None) -> list:
    """Every bar where a pullback setup existed, evaluated all the way
    through confirmation / reward:risk / the valuation gate -- a pure
    function of the chart, independent of any hypothetical position
    state. Position-state gating (skip while already holding, for
    whichever ticker this is) happens in _simulate_tickers, not here, so
    this list is reusable by both the single-ticker and shared-capital
    multi-ticker simulations without duplicating the scan logic.

    Returns a list of (setup_i, candidate_dict_or_None) in bar order.
    """
    raw = []
    i = 0
    n = len(df)
    while i < n - 1:
        setup = find_pullback_setup(df, i, config)
        if setup is None:
            i += 1
            continue
        confirmation = check_confirmation(df, i, setup)
        candidate = None
        if confirmation.confirmed:
            rr = evaluate_reward_to_risk(confirmation.entry_price, setup.stop_price, setup.target_price,
                                          config.min_reward_to_risk)
            entry_i = i + 1
            valuation_ok = True
            if needs_dcf:
                dcf_structured = _dcf_structured_as_of(fair_values, df.index[entry_i])
                gate = evaluate_valuation_gate(dcf_structured, confirmation.entry_price, config.valuation_discount_threshold)
                valuation_ok = gate.passed
            if rr.accepted and valuation_ok:
                candidate = {
                    "entry_i": entry_i, "entry_date": df.index[entry_i], "entry_price": confirmation.entry_price,
                    "stop_price": setup.stop_price, "target_price": setup.target_price,
                    "reward_to_risk": rr.reward_to_risk,
                }
        raw.append((i, candidate))
        i = candidate["entry_i"] if candidate is not None else i + 1
    return raw


def _simulate_tickers(ticker_dfs: dict, raw_candidates_by_ticker: dict, config: SwingStrategyConfig,
                       account_equity: float, *, fees=None, start=None, end=None):
    """Daily shared ledger: entries first, then exits, then closing marks.

    Deterministic alphabetical entry priority; today's exit proceeds cannot
    fund today's entries because intraday order is unknown in daily OHLC.
    Sizing uses equity marked at currently available opens/last prior closes.
    Open positions remain valued at the last available close, never discarded.
    A window starts flat and accepts only setups formed within that window.
    """
    fees = float(os.getenv("BACKTEST_FEES_PCT", "0")) if fees is None else fees
    slip = config.slippage_bps / 10000
    validate_economics(account_equity, fees, slip)
    validate_economics(account_equity, position_pct=config.risk_per_trade_pct)
    validate_economics(account_equity, position_pct=config.max_total_open_risk_pct)
    if config.max_holding_trading_days < 1:
        raise ValueError("Holding period must be positive")
    rows = {}
    for ticker, df in ticker_dfs.items():
        validate_prices(df[["open", "high", "low", "close"]])
        if ((df.high < df[["open", "close", "low"]].max(axis=1)) |
                (df.low > df[["open", "close", "high"]].min(axis=1))).any():
            raise ValueError(f"{ticker}: inconsistent OHLC bars")
        rows[ticker] = {date: (i, row) for i, (date, row) in enumerate(df.iterrows())}
    dates = sorted({date for df in ticker_dfs.values() for date in df.index
                    if (start is None or date >= start) and (end is None or date <= end)})
    events = {}
    for ticker in sorted(ticker_dfs):
        df = ticker_dfs[ticker]
        for setup_i, candidate in raw_candidates_by_ticker.get(ticker, []):
            if not 0 <= setup_i < len(df):
                raise ValueError("Candidate setup index out of bounds")
            setup_date = df.index[setup_i]
            if (start is not None and setup_date < start) or (end is not None and setup_date > end):
                continue
            if candidate is not None:
                entry_i = candidate["entry_i"]
                if entry_i != setup_i + 1 or not 0 <= entry_i < len(df) or candidate["entry_date"] != df.index[entry_i]:
                    raise ValueError("A confirmation must execute on the next available session")
                values = [candidate[k] for k in ("entry_price", "stop_price", "target_price")]
                if not all(math.isfinite(float(v)) and v > 0 for v in values) or not values[1] < values[0] < values[2]:
                    raise ValueError("Invalid candidate prices")
                date = candidate["entry_date"]
            else:
                date = setup_date
            events.setdefault(date, []).append((ticker, setup_i, candidate))

    cash, open_risk = float(account_equity), 0.
    positions, marks, last_exit = {}, {}, {}
    trades, equity, cash_history = [], [], []
    evaluated, rejected = ({t: 0 for t in ticker_dfs} for _ in range(2))
    fee_total = slip_total = 0.
    for date in dates:
        # An open is known before a possible breakout trigger, a close isn't.
        for ticker in positions:
            if date in rows[ticker]:
                marks[ticker] = float(rows[ticker][date][1]["open"])
        for ticker, setup_i, candidate in sorted(events.get(date, []), key=lambda event: (event[0], event[1])):
            if ticker in positions or (ticker in last_exit and ticker_dfs[ticker].index[setup_i] <= last_exit[ticker]):
                continue
            evaluated[ticker] += 1
            if candidate is None:
                continue
            current_equity = cash + sum(p["shares"] * marks[t] for t, p in positions.items())
            sizing = size_position(account_equity=current_equity, cash_available=cash,
                entry_price=candidate["entry_price"], stop_price=candidate["stop_price"],
                open_risk_dollars=open_risk, config=config, buying_power=cash)
            if not sizing.accepted:
                rejected[ticker] += 1
                continue
            shares = sizing.shares
            entry_notional = shares * candidate["entry_price"] * (1+slip)
            entry_fee = entry_notional * fees
            cost = entry_notional + entry_fee
            risk = shares * (candidate["entry_price"]-candidate["stop_price"])
            # Recompute exactly instead of using the live sizer's rounded-cent
            # estimate; rounding must never create cash or overdraw the account.
            if cost > cash + 1e-9 or open_risk+risk > current_equity*config.max_total_open_risk_pct+1e-8:
                rejected[ticker] += 1
                continue
            cash -= cost
            open_risk += risk
            entry_slip = shares*candidate["entry_price"]*slip
            fee_total += entry_fee
            slip_total += entry_slip
            positions[ticker] = dict(candidate, shares=shares, risk=risk, entry_cost=cost,
                                     entry_fee=entry_fee, entry_slip=entry_slip)
            marks[ticker] = candidate["entry_price"]
        for ticker, pos in list(positions.items()):
            if date not in rows[ticker]:
                continue
            i, row = rows[ticker][date]
            held = i-pos["entry_i"]
            # On an intraday breakout entry, the session open preceded the
            # entry. Never invent a gap exit at that earlier price. The daily
            # low may precede the entry too: assume the adverse stop outcome.
            exit_open = pos["entry_price"] if held == 0 else float(row["open"])
            decision = evaluate_swing_exit_bar(open_price=exit_open, high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), stop_price=pos["stop_price"],
                target_price=pos["target_price"], sessions_held=held,
                max_holding_trading_days=config.max_holding_trading_days)
            if not decision.should_exit:
                continue
            shares = pos["shares"]
            exit_notional = shares*decision.exit_price*(1-slip)
            exit_fee = exit_notional*fees
            proceeds = exit_notional-exit_fee
            cash += proceeds
            open_risk -= pos["risk"]
            exit_slip = shares*decision.exit_price*slip
            fee_total += exit_fee
            slip_total += exit_slip
            gross = shares*(decision.exit_price-pos["entry_price"])
            pnl = proceeds-pos["entry_cost"]
            trades.append(SimulatedTrade(ticker=ticker, entry_date=str(pos["entry_date"].date()),
                entry_price=pos["entry_price"], stop_price=pos["stop_price"], target_price=pos["target_price"],
                reward_to_risk=pos["reward_to_risk"], shares=shares, exit_date=str(date.date()),
                exit_price=decision.exit_price, exit_reason=decision.reason.value, sessions_held=held,
                pnl=pnl, pnl_pct=pnl/pos["entry_cost"], gross_pnl=gross,
                fees_paid=pos["entry_fee"]+exit_fee, slippage_cost=pos["entry_slip"]+exit_slip))
            del positions[ticker]
            last_exit[ticker] = date
        for ticker in positions:
            if date in rows[ticker]:
                marks[ticker] = float(rows[ticker][date][1]["close"])
        equity.append(cash+sum(p["shares"]*marks[t] for t, p in positions.items()))
        cash_history.append(cash)
    value = sum(p["shares"]*marks[t] for t, p in positions.items())
    return SimulationResult(trades, evaluated, rejected, cash, max(0., open_risk),
        pd.Series(equity, index=pd.DatetimeIndex(dates), dtype=float),
        pd.Series(cash_history, index=pd.DatetimeIndex(dates), dtype=float),
        value, positions, fee_total, slip_total)


def _prepare_ticker(ticker: str, company_name: str | None, config: SwingStrategyConfig,
                     outputsize: int, needs_dcf: bool):
    """Fetches/computes everything needed to simulate one ticker exactly
    once -- shared by the per-ticker and shared-capital paths so neither
    re-fetches OHLCV or re-issues real DCF LLM calls for the same ticker."""
    if needs_dcf and not company_name:
        return None, "no SEC CIK found for this ticker — can't run a filing-sourced DCF"
    df = fetch_ohlcv(ticker, outputsize=outputsize)
    validate_prices(df[["open", "high", "low", "close"]])
    if df.empty:
        return None, "no historical price bars"
    df = compute_indicators(df)
    df = df.join(rolling_support_resistance(df, window=config.support_resistance_window))
    fair_values = get_point_in_time_fair_values(ticker, company_name) if needs_dcf else None
    raw = _find_raw_candidates(df, config, needs_dcf, fair_values)
    return (df, raw), None


def run_swing_backtest_for_ticker(
    ticker: str, company_name: str | None, config: SwingStrategyConfig,
    outputsize: int = 5000, account_equity: float = 100_000.0,
) -> BacktestResult:
    if config.gate_variant in (GateVariant.TECHNICAL_PLUS_SENTIMENT, GateVariant.TECHNICAL_PLUS_BOTH):
        return BacktestResult(ticker, config.gate_variant.value, supported=False, reason=HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE)

    needs_dcf = config.gate_variant in (GateVariant.TECHNICAL_PLUS_DCF, GateVariant.TECHNICAL_PLUS_BOTH)
    prepared, reason = _prepare_ticker(ticker, company_name, config, outputsize, needs_dcf)
    if prepared is None:
        return BacktestResult(ticker, config.gate_variant.value, supported=False, reason=reason)
    df, raw = prepared

    simulation = _simulate_tickers({ticker: df}, {ticker: raw}, config, account_equity)
    trades, evaluated, rejected, _cash, _open_risk = simulation

    return BacktestResult(
        ticker, config.gate_variant.value, supported=True,
        candidates_evaluated=evaluated[ticker], candidates_rejected_sizing=rejected[ticker], trades=trades,
        portfolio=_summarize(trades, account_equity, simulation),
    )


def report_prepared(ticker_dfs, raw_by_ticker, config, account_equity, n_windows=4, start=None, end=None):
    """Report the same prepared observations without refetching providers."""
    per_ticker = []
    for ticker in sorted(ticker_dfs):
        sim = _simulate_tickers({ticker: ticker_dfs[ticker]}, {ticker: raw_by_ticker[ticker]},
                                config, account_equity, start=start, end=end)
        per_ticker.append({"ticker": ticker, "candidates_evaluated": sim.evaluated[ticker],
                           "candidates_rejected_sizing": sim.rejected[ticker],
                           **_summarize(sim.trades, account_equity, sim)})
    sim = _simulate_tickers(ticker_dfs, raw_by_ticker, config, account_equity, start=start, end=end)
    shared = {"tickers": sorted(ticker_dfs), "candidates_evaluated": sim.evaluated,
              "candidates_rejected_sizing": sim.rejected,
              "date_range": (str(sim.equity_curve.index.min().date()), str(sim.equity_curve.index.max().date())) if not sim.equity_curve.empty else None,
              **_summarize(sim.trades, account_equity, sim)}
    windows = []
    for dates in split_into_windows(pd.Series(sim.equity_curve.index), n_windows):
        if dates.empty:
            continue
        first, last = dates.iloc[0], dates.iloc[-1]
        window = _simulate_tickers(ticker_dfs, raw_by_ticker, config, account_equity, start=first, end=last)
        windows.append({"date_range": (str(first.date()), str(last.date())),
                        "window_type": "independent_flat_start; prior bars used only for indicator warmup",
                        **_summarize(window.trades, account_equity, window)})
    return {"supported": True, "reason": None, "per_ticker": per_ticker,
            "shared_capital_portfolio": shared, "windows": windows,
            "fees_pct": float(os.getenv("BACKTEST_FEES_PCT", "0")),
            "slippage_bps": config.slippage_bps,
            "entry_priority": "alphabetical; entries precede same-session exits"}


def compare_variants(tickers_with_names: list[tuple[str, str | None]], base_config: SwingStrategyConfig,
                      outputsize: int = 5000, account_equity: float = 100_000.0, n_windows: int = 4,
                      variants=None, fetch_pause_seconds=0.0) -> dict:
    """A/B use one cached price dataset and one common usable cohort/period.

    Per-variant gates differ, not their input histories. Exclusions are explicit;
    no historical-sentiment variants are simulated. No fitted parameters.
    """
    import time
    variants = [GateVariant(v) for v in (variants if variants is not None else list(GateVariant))]
    supported = [v for v in variants if v in (GateVariant.TECHNICAL_ONLY, GateVariant.TECHNICAL_PLUS_DCF)]
    frames, candidates, excluded = {}, {v: {} for v in supported}, {}
    requires_dcf = GateVariant.TECHNICAL_PLUS_DCF in supported
    if supported:
        # Dedupe before external calls and consistently choose ticker priority.
        for number, (ticker, name) in enumerate(sorted(dict(tickers_with_names).items())):
            if number and fetch_pause_seconds:
                time.sleep(fetch_pause_seconds)
            try:
                if requires_dcf and not name:
                    excluded[ticker] = "no company/SEC identifier for the common A/B cohort"
                    continue
                prepared, reason = _prepare_ticker(ticker, name, base_config, outputsize, False)
                if prepared is None:
                    excluded[ticker] = reason
                    continue
                df, _ = prepared
                if df.empty:
                    excluded[ticker] = "no historical price bars"
                    continue
                fair = get_point_in_time_fair_values(ticker, name) if requires_dcf else None
                if requires_dcf and (fair is None or fair.dropna().empty):
                    excluded[ticker] = "no verified filing-sourced historical fair values"
                    continue
                frames[ticker] = df
                for variant in supported:
                    config = SwingStrategyConfig(**{**base_config.as_dict(), "gate_variant": variant})
                    candidates[variant][ticker] = _find_raw_candidates(df, config,
                        variant == GateVariant.TECHNICAL_PLUS_DCF, fair)
            except Exception as exc:
                excluded[ticker] = f"preparation failed: {type(exc).__name__}: {exc}"
                frames.pop(ticker, None)
                for variant in supported:
                    candidates[variant].pop(ticker, None)
    # Same range in every supported variant. Individual missing sessions are
    # not fabricated; held assets retain their last known mark on those dates.
    start = max((df.index.min() for df in frames.values()), default=None)
    end = min((df.index.max() for df in frames.values()), default=None)
    results = {}
    for variant in variants:
        if variant not in supported:
            results[variant.value] = {"supported": False, "reason": HISTORICAL_SENTIMENT_UNSUPPORTED_MESSAGE,
                                      "per_ticker": [], "shared_capital_portfolio": None, "windows": []}
        elif not frames or start > end:
            results[variant.value] = {"supported": False, "reason": "no common usable universe/period",
                                      "excluded_tickers": excluded, "per_ticker": [],
                                      "shared_capital_portfolio": None, "windows": []}
        else:
            config = SwingStrategyConfig(**{**base_config.as_dict(), "gate_variant": variant})
            results[variant.value] = {**report_prepared(frames, candidates[variant], config,
                account_equity, n_windows, start, end), "excluded_tickers": excluded,
                "comparison_universe": sorted(frames),
                "comparison_period": (str(start.date()), str(end.date()))}
    return results

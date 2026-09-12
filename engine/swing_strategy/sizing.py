"""
Position sizing and aggregate portfolio-risk enforcement for the
experimental swing strategy.

This is a *risk-based* sizer (risk a fixed % of equity per trade, sized
off the stop distance) — a different model from the live strategy's flat
10%-of-equity notional sizing in functions/market_hours_trading.py. The
two are not meant to blend or be compared line-for-line; this module
never touches MAX_POSITION_PCT or the live sizing path.

Every number this produces is a *plan*, not a guarantee — a stop can gap
through on paper the same way it can in a real market. See
docs/PLANNED_RISK_NOT_GUARANTEED note at the bottom of this file.
"""

import math
from dataclasses import dataclass

from .config import SwingStrategyConfig


@dataclass(frozen=True)
class SizingResult:
    accepted: bool
    shares: float | None
    planned_dollar_risk: float | None
    estimated_cost: float | None
    rejection_reason: str | None


def _round_down_to_increment(qty: float, increment: float) -> float:
    if increment <= 0:
        return qty
    steps = math.floor(qty / increment + 1e-9)  # tiny epsilon guards float noise at exact multiples
    return round(steps * increment, 8)


def size_position(
    *,
    account_equity: float | None,
    cash_available: float | None,
    entry_price: float | None,
    stop_price: float | None,
    open_risk_dollars: float | None,
    config: SwingStrategyConfig,
    min_trade_increment: float = 0.0001,  # Alpaca's typical fractional-share increment; override per-asset when known
    buying_power: float | None = None,
) -> SizingResult:
    """
    risk_budget = account_equity * risk_per_trade_pct
    shares      = risk_budget / (entry_price - stop_price)

    Rejects (fails safe) rather than sizing on a guess whenever equity,
    prices, or the existing open-risk figure aren't actually known —
    trading blind on a stale/missing input is worse than skipping the
    trade for one cycle.
    """
    if account_equity is None or not math.isfinite(account_equity) or account_equity <= 0:
        return SizingResult(False, None, None, None, f"account_equity unavailable or invalid: {account_equity!r}")
    if cash_available is None or not math.isfinite(cash_available):
        return SizingResult(False, None, None, None, f"cash_available unavailable or invalid: {cash_available!r}")
    if entry_price is None or stop_price is None or not math.isfinite(entry_price) or not math.isfinite(stop_price):
        return SizingResult(False, None, None, None, "entry_price/stop_price unavailable or invalid")
    if entry_price <= stop_price:
        return SizingResult(False, None, None, None, "entry_price must be above stop_price for a long position")
    if open_risk_dollars is None or not math.isfinite(open_risk_dollars):
        return SizingResult(False, None, None, None, "existing open-risk figure unavailable — refusing to size blind")

    risk_per_share = entry_price - stop_price
    risk_budget = account_equity * config.risk_per_trade_pct

    max_total_open_risk = account_equity * config.max_total_open_risk_pct
    remaining_risk_capacity = max_total_open_risk - open_risk_dollars
    if remaining_risk_capacity <= 0:
        return SizingResult(
            False, None, None, None,
            f"aggregate open risk ${open_risk_dollars:.2f} already at/over the "
            f"{config.max_total_open_risk_pct * 100:.1f}% cap (${max_total_open_risk:.2f}) — no new swing entries",
        )
    risk_budget = min(risk_budget, remaining_risk_capacity)

    raw_shares = risk_budget / risk_per_share
    shares = _round_down_to_increment(raw_shares, min_trade_increment)
    if shares <= 0:
        return SizingResult(
            False, None, None, None,
            f"risk budget ${risk_budget:.2f} rounds down to 0 shares at ${risk_per_share:.2f} risk/share "
            f"and a {min_trade_increment} share increment",
        )

    slippage_adjusted_entry = entry_price * (1 + config.slippage_bps / 10_000)
    estimated_cost = shares * slippage_adjusted_entry
    planned_dollar_risk = shares * risk_per_share

    if buying_power is not None and math.isfinite(buying_power) and estimated_cost > buying_power:
        return SizingResult(
            False, shares, planned_dollar_risk, estimated_cost,
            f"estimated cost ${estimated_cost:.2f} exceeds buying power ${buying_power:.2f}",
        )
    if estimated_cost > cash_available:
        return SizingResult(
            False, shares, planned_dollar_risk, estimated_cost,
            f"estimated cost ${estimated_cost:.2f} (incl. {config.slippage_bps}bps slippage) "
            f"exceeds available cash ${cash_available:.2f}",
        )

    return SizingResult(True, shares, round(planned_dollar_risk, 2), round(estimated_cost, 2), None)


# PLANNED_RISK_NOT_GUARANTEED: `planned_dollar_risk` above is what this
# trade is *sized* to lose if the stop fills exactly where it's set. It is
# not a ceiling on actual loss — a gap down, a halt, or a fast market can
# fill a stop well past its trigger price. Nothing in this module (or the
# exits/execution modules) should describe this figure as a guaranteed
# maximum loss; every place it's logged or displayed should call it
# "planned risk."

"""
DCF valuation gate for swing-strategy variants B and D.

Finding worth stating plainly: the live gate in engine.entry_exit.signals
computes

    discount = (fair_value - entry_price) / entry_price

and requires discount >= VALUATION_DISCOUNT_THRESHOLD (0.20). That
expression is fair_value/price - 1 — i.e. UPSIDE relative to the current
market price — not the textbook "discount to fair value"
(1 - price/fair_value), even though the live code and its comments call
it a discount. The two are numerically different (fair_value=120,
price=100 gives 20% upside but only 16.7% discount-to-fair-value) and
get further apart as the gap widens. This module computes and logs BOTH,
under their correct names, so the distinction is visible going forward —
and gates on `upside` specifically, to preserve the live strategy's
actual (not nominally-described) semantics for comparability.

Share-count enforcement: same fail-closed rule as the live fix in
engine.entry_exit.signals.evaluate_entry — a DCF is only usable if
share_count_source == "filing" exactly. Missing, "unavailable", or any
other value is rejected, never silently passed through.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ValuationGateResult:
    passed: bool
    upside: float | None  # fair_value / price - 1
    discount_to_fair_value: float | None  # 1 - price / fair_value
    fair_value_per_share: float | None
    share_count_source: str | None
    rejection_reason: str | None


def upside(fair_value: float, price: float) -> float:
    return fair_value / price - 1


def discount_to_fair_value(fair_value: float, price: float) -> float:
    return 1 - price / fair_value


def evaluate_valuation_gate(dcf_structured: dict | None, current_price: float, threshold: float) -> ValuationGateResult:
    if dcf_structured is None:
        return ValuationGateResult(False, None, None, None, None, "no DCF data available")

    share_count_source = dcf_structured.get("share_count_source")
    if share_count_source != "filing":
        return ValuationGateResult(
            False, None, None, None, share_count_source,
            f"DCF share count not filing-sourced (share_count_source={share_count_source!r}) — "
            "missing/invalid DCF data must not silently pass",
        )

    fair_value = dcf_structured.get("estimated_fair_value_per_share")
    try:
        fair_value = float(fair_value)
    except (TypeError, ValueError):
        fair_value = None

    if fair_value is None or not math.isfinite(fair_value) or fair_value <= 0:
        return ValuationGateResult(False, None, None, fair_value, share_count_source, "fair value missing or invalid")
    if current_price is None or not math.isfinite(current_price) or current_price <= 0:
        return ValuationGateResult(False, None, None, fair_value, share_count_source, "current price missing or invalid")

    up = upside(fair_value, current_price)
    disc = discount_to_fair_value(fair_value, current_price)

    if up < threshold:
        return ValuationGateResult(
            False, round(up, 4), round(disc, 4), fair_value, share_count_source,
            f"upside {up * 100:.1f}% below the required {threshold * 100:.0f}%",
        )

    return ValuationGateResult(True, round(up, 4), round(disc, 4), fair_value, share_count_source, None)

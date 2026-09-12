"""
Reward:risk validation for long positions — see engine/swing_strategy/config.py
for min_reward_to_risk. Deliberately its own tiny module: this calculation
gates real order placement, so it should be trivial to unit-test in
isolation and impossible to accidentally skip.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RewardRiskResult:
    accepted: bool
    reward_to_risk: float | None
    rejection_reason: str | None


def _is_finite_positive(x) -> bool:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(x) and x > 0


def evaluate_reward_to_risk(entry_price, stop_price, target_price, min_reward_to_risk: float) -> RewardRiskResult:
    """
    reward_to_risk = (target_price - entry_price) / (entry_price - stop_price)

    Requires stop_price < entry_price < target_price (a long position) and
    all three finite and positive. Never adjusts stop/target to pass —
    callers must not retry with moved prices; a rejection here means don't
    trade this candidate.
    """
    for name, value in (("entry_price", entry_price), ("stop_price", stop_price), ("target_price", target_price)):
        if not _is_finite_positive(value):
            return RewardRiskResult(False, None, f"{name} is missing, non-finite, or non-positive: {value!r}")

    entry_price = float(entry_price)
    stop_price = float(stop_price)
    target_price = float(target_price)

    if not (stop_price < entry_price < target_price):
        return RewardRiskResult(
            False, None,
            f"prices out of order for a long: require stop < entry < target, got "
            f"stop={stop_price}, entry={entry_price}, target={target_price}",
        )

    risk = entry_price - stop_price
    reward = target_price - entry_price
    ratio = reward / risk

    if not math.isfinite(ratio):
        return RewardRiskResult(False, None, f"computed reward:risk is not finite ({ratio!r})")

    if ratio < min_reward_to_risk:
        return RewardRiskResult(
            False, round(ratio, 4),
            f"reward:risk {ratio:.2f} is below the required minimum {min_reward_to_risk:.2f}",
        )

    return RewardRiskResult(True, round(ratio, 4), None)

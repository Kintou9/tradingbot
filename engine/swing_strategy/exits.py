"""
Exit evaluation for the swing strategy — stop-loss, take-profit, and the
previously-unused time-based exit, with an explicit precedence and
explicit handling of the ambiguous "touched both levels in one bar" case.

Precedence, always in this order:
    1. stop_loss      (capital preservation comes first)
    2. take_profit
    3. time_based_exit (only checked if neither price level fired)

This is a deliberate choice, not a default: protecting against further
loss takes priority over locking in a gain when a single bar's data can't
tell you which happened first, and a time limit only exists as a fallback
for a trade that's done neither.

Bar-level ambiguity (a single daily bar's low touches the stop AND its
high touches the target — real, and NOT rare for a bar with a wide
range): daily OHLC alone can't say which was touched first. Resolved by,
in order: (1) a gap at the open already past one level settles it
outright; (2) otherwise, if both are touched intrabar with no way to
order them, assume the stop fired first — never assume the more
favorable outcome for the trader.
"""

from dataclasses import dataclass
from enum import Enum


class SwingExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_BASED_EXIT = "time_based_exit"


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: SwingExitReason | None
    exit_price: float | None
    note: str | None = None


def evaluate_swing_exit_bar(
    *, open_price: float, high: float, low: float, close: float,
    stop_price: float, target_price: float,
    sessions_held: int, max_holding_trading_days: int,
    duplicate_exit_pending: bool = False,
) -> ExitDecision:
    """Backtest-oriented: resolves a single OHLC bar against stop/target/
    time, per the precedence and gap/ambiguity rules in the module
    docstring. Time-based exits are executed at this bar's close (see
    trading_calendar.py's module docstring for the execution-timing
    convention)."""
    if duplicate_exit_pending:
        return ExitDecision(False, None, None, "an exit order is already pending for this position — suppressed duplicate")

    gapped_below_stop = open_price <= stop_price
    gapped_above_target = open_price >= target_price

    if gapped_below_stop:
        return ExitDecision(True, SwingExitReason.STOP_LOSS, open_price, "gap-down through stop at the open")
    if gapped_above_target:
        return ExitDecision(True, SwingExitReason.TAKE_PROFIT, open_price, "gap-up through target at the open")

    touched_stop = low <= stop_price
    touched_target = high >= target_price

    if touched_stop and touched_target:
        return ExitDecision(
            True, SwingExitReason.STOP_LOSS, stop_price,
            "bar touched both stop and target intrabar — order can't be determined from daily OHLC; "
            "assumed the less favorable outcome (stop) rather than the more favorable one",
        )
    if touched_stop:
        return ExitDecision(True, SwingExitReason.STOP_LOSS, stop_price, None)
    if touched_target:
        return ExitDecision(True, SwingExitReason.TAKE_PROFIT, target_price, None)

    if sessions_held >= max_holding_trading_days:
        return ExitDecision(True, SwingExitReason.TIME_BASED_EXIT, close, f"held {sessions_held} sessions, at the {max_holding_trading_days}-session limit")

    return ExitDecision(False, None, None, None)


def evaluate_swing_exit_live(
    *, current_price: float, stop_price: float, target_price: float,
    sessions_held: int, max_holding_trading_days: int,
    duplicate_exit_pending: bool = False,
) -> ExitDecision:
    """Live/paper-trading counterpart — a single current quote rather
    than a completed bar's OHLC, same precedence."""
    if duplicate_exit_pending:
        return ExitDecision(False, None, None, "an exit order is already pending for this position — suppressed duplicate")

    if current_price <= stop_price:
        return ExitDecision(True, SwingExitReason.STOP_LOSS, current_price, None)
    if current_price >= target_price:
        return ExitDecision(True, SwingExitReason.TAKE_PROFIT, current_price, None)
    if sessions_held >= max_holding_trading_days:
        return ExitDecision(True, SwingExitReason.TIME_BASED_EXIT, current_price, f"held {sessions_held} sessions, at the {max_holding_trading_days}-session limit")
    return ExitDecision(False, None, None, None)

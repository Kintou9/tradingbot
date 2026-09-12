"""
Explicit, deterministic technical entry trigger for the experimental
swing strategy: a pullback within an uptrend, confirmed by a breakout
above the pullback bar's high.

No such objective, backtest-reproducible trigger existed anywhere in the
repo before this — engine/price_action_engine/technical_scanner.py hands
computed indicators to an LLM and asks it to narrate an entry range, which
is neither deterministic nor free to re-run thousands of times over
history (see backtesting/backtest_runner.py's own docstring on exactly
this point). This module is the "if none exists, implement one" half of
that requirement, built from the same underlying indicators
(engine.price_action_engine.indicators) the rest of the system trusts.

Definitions (all computed from COMPLETED bars only — nothing here reads a
bar's own not-yet-closed data, and no step assumes an achievable fill at
a price the signal itself was computed from):

Uptrend (context, at bar i):
    close[i] > sma_50[i]  AND  sma_50[i] > sma_50[i - trend_slope_lookback_sessions]
    i.e. price is above its 50-session average AND that average has
    actually been rising over the lookback window — a flat-but-above-SMA50
    tape doesn't count as trending.

Pullback (at bar i, requires the uptrend condition above to also hold at i):
    low[i] <= sma_20[i]  AND  close[i] > sma_50[i]
    i.e. price dipped to/through its 20-session average intraday while the
    close stayed inside the longer uptrend — a shallow, standard
    pullback-to-the-mean read, not a trend break.

Confirmation (checked ONLY on the single session immediately after the
pullback bar — if it doesn't confirm there, the setup lapses and must be
freshly re-detected, it does not stay "armed"):
    trigger_level = pullback bar's high * (1 + confirmation_buffer_pct)
    confirmed if next session's high >= trigger_level

Fill-price assumption on confirmation (never assumes a better fill than
what the bar actually offered):
    if next session's OPEN is already >= trigger_level: the market gapped
        past the trigger before it could be hit at that price — fill is
        the open, not the (stale) trigger level.
    else: fill is the trigger_level itself (a limit/stop right at the
        breakout level) — still subject to the slippage assumption applied
        in sizing.py, and to the live revalidation step in engine.py.

Stop: the pullback bar's own low — the standard placement for this setup
(a break back below the pullback swing invalidates it).
Target: the rolling resistance level as of the pullback bar (see
engine.price_action_engine.indicators.rolling_support_resistance) — the
same "target from technical resistance" convention already used
elsewhere in this project, computed only from bars up to and including
the pullback bar.
"""

import math
from dataclasses import dataclass

import pandas as pd

from .config import SwingStrategyConfig


@dataclass(frozen=True)
class PullbackSetup:
    pullback_date: pd.Timestamp
    pullback_high: float
    stop_price: float
    target_price: float
    trigger_level: float


@dataclass(frozen=True)
class ConfirmationResult:
    confirmed: bool
    confirmation_date: pd.Timestamp | None
    entry_price: float | None
    reason: str | None


def _valid_number(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def find_pullback_setup(df: pd.DataFrame, i: int, config: SwingStrategyConfig) -> PullbackSetup | None:
    """df must already have sma_50, sma_20, resistance columns (see
    engine.price_action_engine.indicators.compute_indicators /
    rolling_support_resistance). i is the pullback-candidate bar's
    positional index — uses only df.iloc[<=i]."""
    lookback = config.trend_slope_lookback_sessions
    if i < lookback:
        return None  # not enough bars before i to compare against for the slope check

    # sma_50 itself is NaN for the first (trend_sma_length - 1) rows of any
    # real OHLCV series (see engine.price_action_engine.indicators —
    # ta.sma pads with NaN until it has a full window) — the _valid_number
    # check on `required` below already rejects those rows, so there's no
    # separate "has trend_sma_length bars of history" check needed here.

    row = df.iloc[i]
    prior_sma = df.iloc[i - lookback]["sma_50"]

    required = [row.get("close"), row.get("low"), row.get("high"), row.get("sma_50"), row.get("sma_20"), prior_sma]
    if any(not _valid_number(v) for v in required):
        return None

    uptrend = row["close"] > row["sma_50"] and row["sma_50"] > prior_sma
    if not uptrend:
        return None

    pullback = row["low"] <= row["sma_20"] and row["close"] > row["sma_50"]
    if not pullback:
        return None

    resistance = row.get("resistance")
    stop_price = float(row["low"])
    if not _valid_number(resistance) or resistance <= row["high"]:
        return None  # no room above the pullback high for a target — not a usable setup
    target_price = float(resistance)

    trigger_level = float(row["high"]) * (1 + config.confirmation_buffer_pct)

    return PullbackSetup(
        pullback_date=df.index[i],
        pullback_high=float(row["high"]),
        stop_price=stop_price,
        target_price=target_price,
        trigger_level=trigger_level,
    )


def check_confirmation(df: pd.DataFrame, pullback_i: int, setup: PullbackSetup) -> ConfirmationResult:
    """Looks at exactly one bar: the session immediately after the
    pullback bar. Never looks further ahead — a setup that isn't
    confirmed the very next session has lapsed (see module docstring)."""
    confirm_i = pullback_i + 1
    if confirm_i >= len(df):
        return ConfirmationResult(False, None, None, "no session yet after the pullback bar to confirm against")

    confirm_row = df.iloc[confirm_i]
    if not _valid_number(confirm_row.get("open")) or not _valid_number(confirm_row.get("high")):
        return ConfirmationResult(False, None, None, "confirmation-session OHLC data unavailable")

    if confirm_row["high"] < setup.trigger_level:
        return ConfirmationResult(
            False, None, None,
            f"next session's high {confirm_row['high']:.2f} never reached the "
            f"trigger level {setup.trigger_level:.2f} — setup lapsed",
        )

    # Gapped past the trigger before it could fill there — fill at the
    # open, never at the stale trigger level (no favorable-execution
    # assumption on a gap).
    fill_price = max(setup.trigger_level, float(confirm_row["open"]))
    return ConfirmationResult(True, df.index[confirm_i], fill_price, None)


def check_confirmation_intraday(setup: PullbackSetup, session_open: float, latest_price: float,
                                 as_of: pd.Timestamp | None = None) -> ConfirmationResult:
    """Live/paper-trading counterpart to check_confirmation — same gap-
    aware fill logic, but against a live quote instead of a completed
    bar, for the session immediately following the pullback bar. This is
    the "revalidate price-sensitive checks immediately before paper order
    submission" step: research freshness (the pullback was detected off
    yesterday's close) does not establish quote freshness, so this must
    be called with a quote pulled right before order submission, not
    reused from earlier in the day."""
    if not _valid_number(session_open) or not _valid_number(latest_price):
        return ConfirmationResult(False, None, None, "live quote unavailable")

    if latest_price < setup.trigger_level:
        return ConfirmationResult(
            False, None, None,
            f"latest price {latest_price:.2f} has not reached the trigger level {setup.trigger_level:.2f}",
        )

    fill_price = max(setup.trigger_level, float(session_open))
    return ConfirmationResult(True, as_of, fill_price, None)

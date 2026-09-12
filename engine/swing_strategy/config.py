"""
Configuration for the experimental days-to-weeks swing-trading strategy.

Everything here is a deliberately separate, parallel system from the
live strategy in engine/entry_exit/signals.py + functions/market_hours_trading.py.
Nothing in this package is wired into the autonomous-trading loop or the
dashboard's manual buy/sell buttons — it only runs when explicitly invoked
(functions/swing_paper_trading.py, backtesting/swing_backtest.py, or the
tests). That is a deliberate safety boundary, not an oversight: the live
loop keeps trading exactly as it did before this package existed.

STRATEGY_VERSION is stamped onto every logged candidate and every
SwingPosition row so that changing a default later doesn't retroactively
reinterpret old paper-trading history.
"""

from dataclasses import dataclass, field
from enum import Enum


STRATEGY_VERSION = "swing-v1-experimental"


class GateVariant(str, Enum):
    """Which of the four requested combinations of gates on top of the
    mandatory technical setup are active. See engine.py for how these are
    combined; see docs at the bottom of this file for the rationale."""

    TECHNICAL_ONLY = "A"
    TECHNICAL_PLUS_DCF = "B"
    TECHNICAL_PLUS_SENTIMENT = "C"
    TECHNICAL_PLUS_BOTH = "D"


@dataclass(frozen=True)
class SwingStrategyConfig:
    # --- Reward:risk ---
    # Experimental default, not a proven number — see conversation record.
    # 1.5 means the target must be at least 1.5x as far from entry as the
    # stop is, i.e. this strategy plans to lose money on more than 40% of
    # trades and still come out ahead if win/loss sizing behaves as planned.
    min_reward_to_risk: float = 1.5

    # --- Position sizing / portfolio risk ---
    # Risk-based sizing (risk a fixed % of equity per trade, sized off the
    # stop distance) rather than the live strategy's flat 10%-of-equity
    # notional sizing — those are different risk models and shouldn't be
    # blended. 0.5% is conservative on purpose for an unproven config.
    risk_per_trade_pct: float = 0.005
    # Sum of planned risk (not notional) across all open swing positions
    # plus any pending swing entry orders may not exceed this.
    max_total_open_risk_pct: float = 0.02

    # --- Time-based exit ---
    # Trading sessions (see trading_calendar.py), not calendar days.
    max_holding_trading_days: int = 20

    # --- Technical entry trigger (see technical_trigger.py) ---
    # Trend context: require the close above SMA50 AND SMA50 itself rising
    # over this many sessions (a flat-but-above-SMA50 market doesn't count
    # as trending for this purpose). trend_sma_length/pullback_sma_length
    # document intent (50/20-period SMAs) but technical_trigger.py reads
    # the fixed sma_50/sma_20 columns engine.price_action_engine.indicators
    # already computes — changing these two numbers alone does not change
    # which column is read; that would need indicators.py to compute a
    # matching column too.
    trend_sma_length: int = 50
    trend_slope_lookback_sessions: int = 5
    # Pullback: the pullback bar's low must reach at or below this shorter
    # SMA while its close stays above the trend SMA above.
    pullback_sma_length: int = 20
    # Confirmation buffer above the pullback bar's high, as a fraction of
    # price, to avoid triggering on a one-tick poke through the level.
    confirmation_buffer_pct: float = 0.0005
    # Stop/target come from the same rolling support/resistance window
    # already used elsewhere (engine.price_action_engine.indicators).
    support_resistance_window: int = 50

    # --- Cost/slippage assumption ---
    # No transaction-cost or slippage model exists anywhere else in this
    # repo (the live backtester runs at fees=0.0) — this is a new,
    # explicit, experimental assumption, not a discovered "existing" one.
    # 5 bps one-way is a conservative placeholder for a liquid large/mid
    # cap name; it is not calibrated to this account's actual fill history.
    slippage_bps: float = 5.0

    # --- Valuation / sentiment gates (variants B, C, D) ---
    valuation_discount_threshold: float = 0.20  # matches the live gate's threshold
    min_sentiment_score: float = 0.0  # matches the live gate's threshold
    research_max_age_hours: int = 24  # matches the live gate's freshness window

    gate_variant: GateVariant = GateVariant.TECHNICAL_ONLY

    strategy_version: str = STRATEGY_VERSION

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["gate_variant"] = self.gate_variant.value
        return d

"""Cheap insurance against accidental drift into restricted territory
while touching backtesting/ -- these constants must stay exactly what
they were before Phase 1 (signal thresholds, DCF formulas, and risk
defaults were explicitly off-limits for this work).
"""
from engine.entry_exit.signals import VALUATION_DISCOUNT_THRESHOLD, MIN_SENTIMENT_SCORE, MIN_TRADE_NOTIONAL
from engine.entry_exit.risk import Limits
from engine.swing_strategy.config import SwingStrategyConfig


def test_live_signal_thresholds_untouched():
    assert VALUATION_DISCOUNT_THRESHOLD == 0.20
    assert MIN_SENTIMENT_SCORE == 0.0
    assert MIN_TRADE_NOTIONAL == 1.0


def test_live_risk_limits_defaults_untouched(monkeypatch):
    for var in ("MAX_TRADES_PER_DAY", "MAX_TOTAL_EXPOSURE_PCT", "MAX_POSITION_PCT",
                "DAILY_LOSS_LIMIT_PCT", "MAX_TRADE_NOTIONAL"):
        monkeypatch.delenv(var, raising=False)
    limits = Limits.from_env()
    assert limits.max_entries == 3
    assert limits.max_exposure_pct == 0.30
    assert limits.max_position_pct == 0.10
    assert limits.daily_loss_pct == 0.02
    assert limits.max_trade == 100.0


def test_swing_strategy_config_defaults_untouched():
    config = SwingStrategyConfig()
    assert config.min_reward_to_risk == 1.5
    assert config.risk_per_trade_pct == 0.005
    assert config.max_total_open_risk_pct == 0.02
    assert config.valuation_discount_threshold == 0.20
    assert config.min_sentiment_score == 0.0
    assert config.slippage_bps == 5.0

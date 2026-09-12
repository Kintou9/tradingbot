import math

from engine.swing_strategy.config import SwingStrategyConfig
from engine.swing_strategy.sizing import size_position


def make_config(**overrides) -> SwingStrategyConfig:
    return SwingStrategyConfig(**overrides)


def test_basic_sizing_matches_risk_budget():
    # equity 100,000 * 0.5% = $500 risk budget; risk/share = 100-95=5 -> 100 shares
    config = make_config(risk_per_trade_pct=0.005, max_total_open_risk_pct=0.02, slippage_bps=0)
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=0, config=config, min_trade_increment=1.0,
    )
    assert result.accepted
    assert result.shares == 100
    assert result.planned_dollar_risk == 500.0


def test_rounds_down_to_increment_never_up():
    config = make_config(risk_per_trade_pct=0.005, slippage_bps=0)
    # risk budget $500 / $5 risk-per-share = 100.0 exactly with increment 1 -> fine;
    # use an increment that doesn't divide evenly to confirm it floors.
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95.3,
        open_risk_dollars=0, config=config, min_trade_increment=1.0,
    )
    # risk/share = 4.7, raw shares = 500/4.7 = 106.38... -> floors to 106
    assert result.accepted
    assert result.shares == 106
    assert result.shares * 4.7 <= 500.0 + 1e-6


def test_rejects_insufficient_cash():
    config = make_config(risk_per_trade_pct=0.5, slippage_bps=0)  # deliberately huge to force a big size
    result = size_position(
        account_equity=100_000, cash_available=100, entry_price=100, stop_price=95,
        open_risk_dollars=0, config=config, min_trade_increment=1.0,
    )
    assert not result.accepted
    assert "cash" in result.rejection_reason


def test_rejects_insufficient_buying_power_even_with_cash():
    config = make_config(risk_per_trade_pct=0.005, slippage_bps=0)
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=0, config=config, min_trade_increment=1.0, buying_power=10,
    )
    assert not result.accepted
    assert "buying power" in result.rejection_reason


def test_aggregate_open_risk_cap_reduces_budget():
    # cap is 2% of 100,000 = $2000. Already $1900 of open risk -> only
    # $100 of headroom left, overriding the per-trade 0.5% ($500) budget.
    config = make_config(risk_per_trade_pct=0.005, max_total_open_risk_pct=0.02, slippage_bps=0)
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=1900, config=config, min_trade_increment=1.0,
    )
    assert result.accepted
    assert result.planned_dollar_risk <= 100.0 + 1e-6


def test_aggregate_open_risk_cap_blocks_new_entries_when_exhausted():
    config = make_config(risk_per_trade_pct=0.005, max_total_open_risk_pct=0.02)
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=2000, config=config, min_trade_increment=1.0,
    )
    assert not result.accepted
    assert "aggregate open risk" in result.rejection_reason


def test_fails_safe_on_missing_equity():
    config = make_config()
    result = size_position(
        account_equity=None, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=0, config=config,
    )
    assert not result.accepted
    assert "equity" in result.rejection_reason


def test_fails_safe_on_missing_open_risk_figure():
    config = make_config()
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=None, config=config,
    )
    assert not result.accepted
    assert "open-risk" in result.rejection_reason


def test_fails_safe_on_nonfinite_equity():
    config = make_config()
    result = size_position(
        account_equity=math.nan, cash_available=50_000, entry_price=100, stop_price=95,
        open_risk_dollars=0, config=config,
    )
    assert not result.accepted


def test_rejects_entry_at_or_below_stop():
    config = make_config()
    result = size_position(
        account_equity=100_000, cash_available=50_000, entry_price=95, stop_price=95,
        open_risk_dollars=0, config=config,
    )
    assert not result.accepted


def test_slippage_increases_estimated_cost():
    config_no_slip = make_config(slippage_bps=0)
    config_with_slip = make_config(slippage_bps=50)  # 0.5%
    no_slip = size_position(account_equity=100_000, cash_available=50_000, entry_price=100,
                             stop_price=95, open_risk_dollars=0, config=config_no_slip, min_trade_increment=1.0)
    with_slip = size_position(account_equity=100_000, cash_available=50_000, entry_price=100,
                               stop_price=95, open_risk_dollars=0, config=config_with_slip, min_trade_increment=1.0)
    assert with_slip.estimated_cost > no_slip.estimated_cost

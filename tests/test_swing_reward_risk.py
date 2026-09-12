import math

from engine.swing_strategy.reward_risk import evaluate_reward_to_risk


def test_accepts_when_ratio_meets_minimum():
    # risk = 100 - 95 = 5, reward = 107.5 - 100 = 7.5, ratio = 1.5
    result = evaluate_reward_to_risk(entry_price=100, stop_price=95, target_price=107.5, min_reward_to_risk=1.5)
    assert result.accepted
    assert result.reward_to_risk == 1.5
    assert result.rejection_reason is None


def test_rejects_when_ratio_below_minimum():
    result = evaluate_reward_to_risk(entry_price=100, stop_price=95, target_price=104, min_reward_to_risk=1.5)
    assert not result.accepted
    assert result.reward_to_risk == 0.8
    assert "below" in result.rejection_reason


def test_rejects_out_of_order_prices_stop_above_entry():
    result = evaluate_reward_to_risk(entry_price=100, stop_price=101, target_price=110, min_reward_to_risk=1.0)
    assert not result.accepted
    assert result.reward_to_risk is None
    assert "out of order" in result.rejection_reason


def test_rejects_out_of_order_prices_target_below_entry():
    result = evaluate_reward_to_risk(entry_price=100, stop_price=95, target_price=99, min_reward_to_risk=1.0)
    assert not result.accepted
    assert "out of order" in result.rejection_reason


def test_rejects_missing_price():
    result = evaluate_reward_to_risk(entry_price=None, stop_price=95, target_price=110, min_reward_to_risk=1.0)
    assert not result.accepted
    assert "missing" in result.rejection_reason


def test_rejects_nonfinite_price():
    result = evaluate_reward_to_risk(entry_price=math.nan, stop_price=95, target_price=110, min_reward_to_risk=1.0)
    assert not result.accepted
    assert result.reward_to_risk is None


def test_rejects_non_numeric_price():
    result = evaluate_reward_to_risk(entry_price="not a number", stop_price=95, target_price=110, min_reward_to_risk=1.0)
    assert not result.accepted


def test_rejects_zero_or_negative_price():
    result = evaluate_reward_to_risk(entry_price=0, stop_price=-5, target_price=10, min_reward_to_risk=1.0)
    assert not result.accepted


def test_never_mutates_inputs_to_pass():
    # Sanity check on the contract: calling twice with the same (failing)
    # inputs must give the same rejection both times — nothing about a
    # failed call should adjust stop/target to make a retry pass.
    first = evaluate_reward_to_risk(entry_price=100, stop_price=95, target_price=101, min_reward_to_risk=1.5)
    second = evaluate_reward_to_risk(entry_price=100, stop_price=95, target_price=101, min_reward_to_risk=1.5)
    assert first == second
    assert not first.accepted

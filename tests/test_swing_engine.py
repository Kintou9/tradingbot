from datetime import datetime, timezone

from engine.swing_strategy.config import SwingStrategyConfig, GateVariant
from engine.swing_strategy.engine import evaluate_swing_candidate
from engine.swing_strategy.technical_trigger import PullbackSetup, ConfirmationResult


def _setup_and_confirmation(entry=110.0, stop=95.0, target=140.0):
    setup = PullbackSetup(pullback_date=datetime.now(timezone.utc), pullback_high=104,
                           stop_price=stop, target_price=target, trigger_level=104)
    confirmation = ConfirmationResult(confirmed=True, confirmation_date=datetime.now(timezone.utc), entry_price=entry, reason=None)
    return setup, confirmation


def _common_kwargs(**overrides):
    kwargs = dict(
        ticker="TEST", account_equity=100_000, cash_available=50_000, open_risk_dollars=0,
        source_data_timestamps={"ohlcv": datetime.now(timezone.utc)}, min_trade_increment=1.0,
    )
    kwargs.update(overrides)
    return kwargs


def test_variant_a_accepts_on_technical_alone():
    setup, confirmation = _setup_and_confirmation()
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_ONLY, min_reward_to_risk=1.5)
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, **_common_kwargs())
    assert candidate.accepted
    assert candidate.dcf_fair_value is None
    assert candidate.sentiment_avg is None


def test_variant_b_rejects_without_dcf_even_with_good_technical():
    setup, confirmation = _setup_and_confirmation()
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_DCF, min_reward_to_risk=1.5)
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, dcf_structured=None, **_common_kwargs())
    assert not candidate.accepted
    assert "DCF gate" in candidate.rejection_reason


def test_variant_b_rejects_dcf_with_bad_share_count_source():
    setup, confirmation = _setup_and_confirmation()
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_DCF, min_reward_to_risk=1.5)
    dcf = {"estimated_fair_value_per_share": 200, "share_count_source": "unavailable"}
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, dcf_structured=dcf, **_common_kwargs())
    assert not candidate.accepted


def test_variant_b_accepts_with_good_filing_sourced_dcf():
    setup, confirmation = _setup_and_confirmation(entry=100.0)
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_DCF, min_reward_to_risk=1.5, valuation_discount_threshold=0.20)
    dcf = {"estimated_fair_value_per_share": 130, "share_count_source": "filing"}
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, dcf_structured=dcf, **_common_kwargs())
    assert candidate.accepted
    assert candidate.dcf_upside == 0.3


def test_variant_c_rejects_on_missing_news_not_neutral():
    setup, confirmation = _setup_and_confirmation()
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_SENTIMENT, min_reward_to_risk=1.5)
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, sentiment_scores=[], **_common_kwargs())
    assert not candidate.accepted
    assert "sentiment gate" in candidate.rejection_reason


def test_variant_d_requires_both_gates_to_pass():
    setup, confirmation = _setup_and_confirmation(entry=100.0)
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_BOTH, min_reward_to_risk=1.5, valuation_discount_threshold=0.20)
    good_dcf = {"estimated_fair_value_per_share": 130, "share_count_source": "filing"}

    missing_sentiment = evaluate_swing_candidate(
        setup=setup, confirmation=confirmation, config=config, dcf_structured=good_dcf, sentiment_scores=[], **_common_kwargs()
    )
    assert not missing_sentiment.accepted

    both_ok = evaluate_swing_candidate(
        setup=setup, confirmation=confirmation, config=config, dcf_structured=good_dcf, sentiment_scores=[0.3, 0.4], **_common_kwargs()
    )
    assert both_ok.accepted


def test_rejects_when_no_setup_found():
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_ONLY)
    candidate = evaluate_swing_candidate(setup=None, confirmation=None, config=config, **_common_kwargs())
    assert not candidate.accepted
    assert "no pullback" in candidate.rejection_reason


def test_rejects_when_setup_found_but_not_confirmed():
    setup, _ = _setup_and_confirmation()
    unconfirmed = ConfirmationResult(confirmed=False, confirmation_date=None, entry_price=None, reason="never broke out")
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_ONLY)
    candidate = evaluate_swing_candidate(setup=setup, confirmation=unconfirmed, config=config, **_common_kwargs())
    assert not candidate.accepted
    assert candidate.rejection_reason == "never broke out"


def test_llm_confidence_is_logged_but_never_affects_acceptance():
    setup, confirmation = _setup_and_confirmation()
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_ONLY, min_reward_to_risk=1.5)
    low_conf = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, llm_confidence=5, **_common_kwargs())
    high_conf = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, llm_confidence=95, **_common_kwargs())
    assert low_conf.accepted and high_conf.accepted
    assert low_conf.llm_confidence == 5
    assert high_conf.llm_confidence == 95
    # entry/stop/target/sizing identical regardless of confidence
    assert low_conf.shares == high_conf.shares
    assert low_conf.entry_price == high_conf.entry_price


def test_rejects_on_failed_reward_to_risk_before_gates_checked():
    # entry 110, stop 95 -> risk 15; target 118 -> reward 8 -> ratio 0.53, fails 1.5 minimum
    setup, confirmation = _setup_and_confirmation(entry=110.0, stop=95.0, target=118.0)
    config = SwingStrategyConfig(gate_variant=GateVariant.TECHNICAL_PLUS_BOTH, min_reward_to_risk=1.5)
    candidate = evaluate_swing_candidate(setup=setup, confirmation=confirmation, config=config, **_common_kwargs())
    assert not candidate.accepted
    assert "reward:risk" in candidate.rejection_reason

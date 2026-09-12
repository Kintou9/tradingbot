"""
Combines the technical trigger (mandatory), reward:risk validation
(mandatory), the optional DCF/sentiment gates (variant-dependent), and
position sizing into one entry decision — and logs every candidate,
accepted or rejected, with the fields required for later evaluation.

Variant semantics (config.GateVariant):
    A  technical setup only
    B  technical setup + DCF gate
    C  technical setup + sentiment gate
    D  technical setup + both gates

The technical trigger and reward:risk check are NEVER optional — they
apply to all four variants, matching "keep no-downtrend as a filter" plus
"require an objective entry trigger for the experimental strategy" as a
baseline all variants share, with DCF/sentiment as additive screens on
top for the comparison in point 6.

LLM confidence (from the live research pipeline, if supplied) is carried
through purely for logging/analysis — nothing in this module reads it to
accept, reject, resize, or reprice a candidate.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import SwingStrategyConfig, GateVariant
from .reward_risk import evaluate_reward_to_risk
from .sizing import size_position, SizingResult
from .technical_trigger import ConfirmationResult, PullbackSetup
from .valuation import evaluate_valuation_gate, ValuationGateResult
from .sentiment_gate import evaluate_sentiment_gate, SentimentGateResult


@dataclass
class SwingCandidate:
    ticker: str
    variant: str
    strategy_version: str
    accepted: bool
    rejection_reason: str | None

    signal_timestamp: datetime
    source_data_timestamps: dict

    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    reward_to_risk: float | None

    shares: float | None
    planned_dollar_risk: float | None
    estimated_cost: float | None

    dcf_fair_value: float | None = None
    dcf_upside: float | None = None
    dcf_discount_to_fair_value: float | None = None
    dcf_share_count_source: str | None = None

    sentiment_avg: float | None = None
    sentiment_article_count: int | None = None

    llm_confidence: float | None = None  # informational only — see module docstring


def evaluate_swing_candidate(
    *,
    ticker: str,
    setup: PullbackSetup | None,
    confirmation: ConfirmationResult | None,
    config: SwingStrategyConfig,
    account_equity: float | None,
    cash_available: float | None,
    open_risk_dollars: float | None,
    source_data_timestamps: dict,
    dcf_structured: dict | None = None,
    sentiment_scores: list[float] | None = None,
    llm_confidence: float | None = None,
    min_trade_increment: float = 0.0001,
    buying_power: float | None = None,
    now: datetime | None = None,
) -> SwingCandidate:
    now = now or datetime.now(timezone.utc)

    def rejected(reason: str, **extra) -> SwingCandidate:
        defaults = dict(
            entry_price=None, stop_price=None, target_price=None, reward_to_risk=None,
            shares=None, planned_dollar_risk=None, estimated_cost=None,
        )
        defaults.update(extra)
        return SwingCandidate(
            ticker=ticker, variant=config.gate_variant.value, strategy_version=config.strategy_version,
            accepted=False, rejection_reason=reason,
            signal_timestamp=now, source_data_timestamps=source_data_timestamps,
            llm_confidence=llm_confidence, **defaults,
        )

    if setup is None:
        return rejected("no pullback-in-uptrend setup detected")
    if confirmation is None or not confirmation.confirmed:
        return rejected(confirmation.reason if confirmation else "confirmation not evaluated")

    entry_price = confirmation.entry_price
    stop_price = setup.stop_price
    target_price = setup.target_price

    rr = evaluate_reward_to_risk(entry_price, stop_price, target_price, config.min_reward_to_risk)
    if not rr.accepted:
        return rejected(rr.rejection_reason)

    dcf_gate: ValuationGateResult | None = None
    if config.gate_variant in (GateVariant.TECHNICAL_PLUS_DCF, GateVariant.TECHNICAL_PLUS_BOTH):
        dcf_gate = evaluate_valuation_gate(dcf_structured, entry_price, config.valuation_discount_threshold)
        if not dcf_gate.passed:
            return rejected(
                f"DCF gate: {dcf_gate.rejection_reason}", reward_to_risk=rr.reward_to_risk,
                entry_price=entry_price, stop_price=stop_price, target_price=target_price,
                dcf_fair_value=dcf_gate.fair_value_per_share, dcf_upside=dcf_gate.upside,
                dcf_discount_to_fair_value=dcf_gate.discount_to_fair_value,
                dcf_share_count_source=dcf_gate.share_count_source,
            )

    sentiment_gate: SentimentGateResult | None = None
    if config.gate_variant in (GateVariant.TECHNICAL_PLUS_SENTIMENT, GateVariant.TECHNICAL_PLUS_BOTH):
        sentiment_gate = evaluate_sentiment_gate(sentiment_scores or [], config.min_sentiment_score)
        if not sentiment_gate.passed:
            return rejected(
                f"sentiment gate: {sentiment_gate.rejection_reason}", reward_to_risk=rr.reward_to_risk,
                entry_price=entry_price, stop_price=stop_price, target_price=target_price,
                dcf_fair_value=dcf_gate.fair_value_per_share if dcf_gate else None,
                dcf_upside=dcf_gate.upside if dcf_gate else None,
                dcf_discount_to_fair_value=dcf_gate.discount_to_fair_value if dcf_gate else None,
                dcf_share_count_source=dcf_gate.share_count_source if dcf_gate else None,
                sentiment_avg=sentiment_gate.avg_sentiment, sentiment_article_count=sentiment_gate.article_count,
            )

    sizing: SizingResult = size_position(
        account_equity=account_equity, cash_available=cash_available,
        entry_price=entry_price, stop_price=stop_price, open_risk_dollars=open_risk_dollars,
        config=config, min_trade_increment=min_trade_increment, buying_power=buying_power,
    )
    if not sizing.accepted:
        return rejected(
            f"sizing: {sizing.rejection_reason}", reward_to_risk=rr.reward_to_risk,
            entry_price=entry_price, stop_price=stop_price, target_price=target_price,
            shares=sizing.shares, planned_dollar_risk=sizing.planned_dollar_risk, estimated_cost=sizing.estimated_cost,
            dcf_fair_value=dcf_gate.fair_value_per_share if dcf_gate else None,
            dcf_upside=dcf_gate.upside if dcf_gate else None,
            dcf_discount_to_fair_value=dcf_gate.discount_to_fair_value if dcf_gate else None,
            dcf_share_count_source=dcf_gate.share_count_source if dcf_gate else None,
            sentiment_avg=sentiment_gate.avg_sentiment if sentiment_gate else None,
            sentiment_article_count=sentiment_gate.article_count if sentiment_gate else None,
        )

    return SwingCandidate(
        ticker=ticker, variant=config.gate_variant.value, strategy_version=config.strategy_version,
        accepted=True, rejection_reason=None,
        signal_timestamp=now, source_data_timestamps=source_data_timestamps,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price, reward_to_risk=rr.reward_to_risk,
        shares=sizing.shares, planned_dollar_risk=sizing.planned_dollar_risk, estimated_cost=sizing.estimated_cost,
        dcf_fair_value=dcf_gate.fair_value_per_share if dcf_gate else None,
        dcf_upside=dcf_gate.upside if dcf_gate else None,
        dcf_discount_to_fair_value=dcf_gate.discount_to_fair_value if dcf_gate else None,
        dcf_share_count_source=dcf_gate.share_count_source if dcf_gate else None,
        sentiment_avg=sentiment_gate.avg_sentiment if sentiment_gate else None,
        sentiment_article_count=sentiment_gate.article_count if sentiment_gate else None,
        llm_confidence=llm_confidence,
    )

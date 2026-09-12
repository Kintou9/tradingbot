"""
Entry/exit signal engine — Section 4 of the requirements doc.
This is where all the other modules' outputs get combined into an actual
buy/sell decision, and where risk management (Section 7) has final say.
"""

from dataclasses import dataclass
from enum import Enum

# Section 2: only flag a buy if price is 20%+ below calculated fair value.
VALUATION_DISCOUNT_THRESHOLD = 0.20

# Section 4: require neutral-or-better sentiment, not just "not bearish".
MIN_SENTIMENT_SCORE = 0.0

# Alpaca rejects fractional orders below $1 notional — skip signals that
# would size to less than that rather than let order placement fail later.
MIN_TRADE_NOTIONAL = 1.0


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TradeSignal:
    ticker: str
    action: SignalAction
    reason: str  # "valuation_trigger", "stop_loss", "time_based_exit", etc.
    confidence: float


def evaluate_entry(
    ticker: str,
    valuation_result: dict,
    technical_result: dict,
    sentiment_score: float,
    max_position_pct: float,
    account_equity: float,
) -> TradeSignal | None:
    """
    Combine valuation + technical + sentiment signals into an entry decision.

    valuation_result: the "structured" dict from engine.valuation_engine.dcf.run_dcf
        (keys: estimated_fair_value_per_share, wacc, terminal_growth_rate, ...)
    technical_result: the "structured" dict from
        engine.price_action_engine.technical_scanner.run_technical_scan
        (keys: trend, entry_price, stop_loss, target_1, target_2,
        probability_estimate, mean_reversion_bias — the LLM's own read of
        the z-score/%B mean-reversion context, already folded into
        probability_estimate; not gated on separately here)
    sentiment_score: -1.0 (very bearish) to 1.0 (very bullish), see
        engine.news_sentiment.sentiment.score_sentiment
    """
    fair_value = valuation_result.get("estimated_fair_value_per_share")
    entry_price = technical_result.get("entry_price")
    if not fair_value or not entry_price or entry_price <= 0:
        return None

    # A DCF whose per-share figure rests on a share count the model pulled
    # from memory rather than the filing is unreliable by multiples — don't
    # trade the discount it implies. Fail closed on anything except the
    # explicit "filing" value: that covers both an explicit "unavailable"
    # AND every DCF note cached before this field existed at all (reproduced
    # live 2026-09-11 — a pre-fix cached NOW note showed a 418% "discount"
    # from exactly this kind of stale guessed share count).
    if valuation_result.get("share_count_source") != "filing":
        return None

    if technical_result.get("trend") == "down":
        return None

    if sentiment_score < MIN_SENTIMENT_SCORE:
        return None

    discount = (fair_value - entry_price) / entry_price
    if discount < VALUATION_DISCOUNT_THRESHOLD:
        return None

    if account_equity * max_position_pct < MIN_TRADE_NOTIONAL:
        return None

    probability_estimate = technical_result.get("probability_estimate", 0)
    confidence = min(
        1.0,
        0.5 * min(1.0, discount / VALUATION_DISCOUNT_THRESHOLD)
        + 0.5 * (probability_estimate / 100),
    )

    return TradeSignal(ticker, SignalAction.BUY, "valuation_trigger", confidence=confidence)


def evaluate_exit(
    ticker: str,
    entry_price: float,
    current_price: float,
    stop_loss_price: float,
    take_profit_price: float,
) -> TradeSignal | None:
    """Exit logic is separate from entry — value re-rating, stop-loss,
    and time-based exits are each distinct triggers (Section 4)."""
    if current_price <= stop_loss_price:
        return TradeSignal(ticker, SignalAction.SELL, "stop_loss", confidence=1.0)
    if current_price >= take_profit_price:
        return TradeSignal(ticker, SignalAction.SELL, "take_profit", confidence=1.0)
    return None

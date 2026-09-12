"""
Sentiment gate for swing-strategy variants C and D.

Deliberate divergence from the live strategy: functions.market_hours_trading
._avg_sentiment returns 0.0 (neutral) when there's no recent news for a
ticker, which means "we don't know" and "confirmed neutral" are
indistinguishable there. This gate treats an empty news window as missing
data and rejects, per the explicit requirement — a ticker nobody's
written about recently doesn't get a free pass through a sentiment
filter that's supposed to be screening for bad news.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SentimentGateResult:
    passed: bool
    avg_sentiment: float | None
    article_count: int
    rejection_reason: str | None


def evaluate_sentiment_gate(scores: list[float], min_sentiment_score: float) -> SentimentGateResult:
    """scores: sentiment scores (-1..1) for news items in the freshness
    window, already filtered to real articles — pass an empty list, not a
    padded/defaulted one, when there's no news."""
    valid_scores = [s for s in scores if s is not None and math.isfinite(s)]

    if not valid_scores:
        return SentimentGateResult(False, None, 0, "no recent news available — treated as missing data, not neutral")

    avg = sum(valid_scores) / len(valid_scores)
    if avg < min_sentiment_score:
        return SentimentGateResult(
            False, round(avg, 4), len(valid_scores),
            f"average sentiment {avg:.2f} below the required {min_sentiment_score:.2f}",
        )

    return SentimentGateResult(True, round(avg, 4), len(valid_scores), None)

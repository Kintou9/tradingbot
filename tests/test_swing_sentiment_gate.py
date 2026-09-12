from engine.swing_strategy.sentiment_gate import evaluate_sentiment_gate


def test_missing_news_is_rejected_not_treated_as_neutral():
    # The live strategy defaults an empty news window to 0.0 (neutral),
    # which would pass a >=0.0 gate. This gate must NOT do that.
    result = evaluate_sentiment_gate([], min_sentiment_score=0.0)
    assert not result.passed
    assert result.avg_sentiment is None
    assert "missing data" in result.rejection_reason


def test_passes_on_sufficiently_positive_average():
    result = evaluate_sentiment_gate([0.2, 0.4, -0.1], min_sentiment_score=0.0)
    assert result.passed
    assert result.avg_sentiment == round((0.2 + 0.4 - 0.1) / 3, 4)
    assert result.article_count == 3


def test_rejects_below_threshold_average():
    result = evaluate_sentiment_gate([-0.5, -0.3], min_sentiment_score=0.0)
    assert not result.passed
    assert result.avg_sentiment < 0


def test_ignores_nonfinite_scores_but_keeps_valid_ones():
    result = evaluate_sentiment_gate([0.5, float("nan"), None], min_sentiment_score=0.0)
    assert result.passed
    assert result.article_count == 1
    assert result.avg_sentiment == 0.5


def test_all_invalid_scores_treated_as_missing():
    result = evaluate_sentiment_gate([float("nan"), None], min_sentiment_score=0.0)
    assert not result.passed
    assert "missing data" in result.rejection_reason

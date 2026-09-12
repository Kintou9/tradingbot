from engine.swing_strategy.valuation import evaluate_valuation_gate, upside, discount_to_fair_value


def test_upside_and_discount_are_numerically_different():
    fv, price = 120.0, 100.0
    assert round(upside(fv, price), 4) == 0.2
    assert round(discount_to_fair_value(fv, price), 4) == round(1 - 100 / 120, 4)
    assert upside(fv, price) != discount_to_fair_value(fv, price)


def test_passes_when_filing_sourced_and_upside_meets_threshold():
    dcf = {"estimated_fair_value_per_share": 130, "share_count_source": "filing"}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert result.passed
    assert result.upside == 0.3
    assert result.share_count_source == "filing"


def test_rejects_missing_dcf_entirely():
    result = evaluate_valuation_gate(None, current_price=100, threshold=0.20)
    assert not result.passed
    assert "no DCF data" in result.rejection_reason


def test_rejects_unavailable_share_count_source():
    dcf = {"estimated_fair_value_per_share": 200, "share_count_source": "unavailable"}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert not result.passed
    assert "share count" in result.rejection_reason


def test_rejects_missing_share_count_source_field_entirely():
    # Simulates a DCF cached before the field existed at all — must fail
    # closed, exactly like the live evaluate_entry fix.
    dcf = {"estimated_fair_value_per_share": 500}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert not result.passed


def test_rejects_when_upside_below_threshold():
    dcf = {"estimated_fair_value_per_share": 105, "share_count_source": "filing"}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert not result.passed
    assert result.upside == 0.05
    assert "below" in result.rejection_reason


def test_rejects_invalid_fair_value():
    dcf = {"estimated_fair_value_per_share": None, "share_count_source": "filing"}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert not result.passed


def test_rejects_negative_fair_value():
    dcf = {"estimated_fair_value_per_share": -50, "share_count_source": "filing"}
    result = evaluate_valuation_gate(dcf, current_price=100, threshold=0.20)
    assert not result.passed

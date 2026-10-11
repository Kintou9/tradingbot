"""Independent flat-start window semantics and temporal boundary regressions."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backtesting.primary_state import ExpirationReason, PrimarySimulationConfig, ValuationMode
from backtesting.primary_windows import (
    WindowSpec, chronological_windows, prepare_window, run_primary_windows, run_window,
    validate_windows, window_spec,
)
from tests.test_primary_strategy import dataset, value


def config(data, cash=1000, allocation=1, **kwargs):
    return PrimarySimulationConfig(cash, allocation, valuation_mode=data.valuation_mode,
                                   calendar_policy=data.metadata["calendar_source"], **kwargs)


def test_strict_inclusive_observed_boundaries_and_warmup():
    data = dataset()
    spec = window_spec("warm", data.sessions[49], data.sessions[52])
    prepared = prepare_window(data, spec)
    assert prepared.sessions == data.sessions[49:53]
    assert prepared.prices_by_symbol["A"].iloc[0]["sma_50"] == data.prices_by_symbol["A"].iloc[49]["sma_50"]
    report = run_window(data, config(data), spec)
    assert report["window"]["requested_start"] == report["window"]["effective_start"]
    assert report["window"]["requested_end"] == report["window"]["effective_end"]
    assert report["window"]["warmup_observation_count"] == 49
    assert report["decisions"][0]["at"]["session"] == data.sessions[49].isoformat()
    assert report["fills"][0]["at"]["session"] == data.sessions[50].isoformat()
    assert all(fill["at"]["session"] >= data.sessions[49].isoformat() for fill in report["fills"])


@pytest.mark.parametrize("which", ["start", "end"])
def test_non_observed_boundaries_are_rejected_without_shifting(which):
    data = dataset()
    values = dict(start=data.sessions[4], end=data.sessions[14])
    values[which] += timedelta(days=1)  # both selected boundaries are Fridays
    with pytest.raises(ValueError, match=f"{which} is not an observed session"):
        prepare_window(data, window_spec("weekend", **values))


@pytest.mark.parametrize("bad", ["reverse", "empty_before", "empty_after", "invalid_date", "invalid_id"])
def test_invalid_window_definitions(bad):
    data = dataset()
    if bad == "reverse":
        with pytest.raises(ValueError, match="precedes"):
            window_spec("bad", data.sessions[4], data.sessions[3])
    elif bad == "empty_before":
        day = data.sessions[0] - timedelta(days=1)
        with pytest.raises(ValueError, match="not an observed"):
            prepare_window(data, window_spec("bad", day, data.sessions[2]))
    elif bad == "empty_after":
        day = data.sessions[-1] + timedelta(days=1)
        with pytest.raises(ValueError, match="not an observed"):
            prepare_window(data, window_spec("bad", data.sessions[-2], day))
    elif bad == "invalid_date":
        with pytest.raises(ValueError, match="ISO"):
            window_spec("bad", "03/01/2023", "2023-03-02")
    else:
        with pytest.raises(ValueError, match="Window ID"):
            window_spec("unsafe id", data.sessions[0], data.sessions[1])


def test_duplicate_ids_and_empty_schedule_rejected():
    data = dataset()
    one = window_spec("same", data.sessions[0], data.sessions[1])
    two = window_spec("same", data.sessions[2], data.sessions[3])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_windows(data, [one, two])
    with pytest.raises(ValueError, match="At least one"):
        validate_windows(data, [])


def test_canonical_order_and_fresh_state_for_each_window():
    data = dataset()
    early = window_spec("early", data.sessions[49], data.sessions[51])
    late = window_spec("late", data.sessions[52], data.sessions[55])
    reports = run_primary_windows(data, config(data), [late, early])
    assert [report["window"]["window_id"] for report in reports] == ["early", "late"]
    for report in reports:
        assert report["summary"]["starting_capital"] == 1000
        assert report["portfolio_history"][0]["cash"] == 1000
        assert report["portfolio_history"][0]["realized_net_pnl"] == 0
        assert report["portfolio_history"][0]["cumulative_fees"] == 0
    assert reports[0]["summary"]["open_position_count"] == 1
    assert reports[1]["fills"][0]["side"] == "buy"
    assert reports[1]["fills"][0]["quantity"] == 10
    assert not any(order["side"] == "sell" for order in reports[1]["orders"])


def test_no_cross_window_cash_or_fee_leakage():
    data = dataset(changes={('A', 51): (100, 125), ('A', 52): (120, 120)})
    cfg = config(data, allocation=.5, fee_pct=.01, slippage_pct=.01)
    first = window_spec("first", data.sessions[49], data.sessions[52])
    second = window_spec("second", data.sessions[53], data.sessions[55])
    reports = run_primary_windows(data, cfg, [first, second])
    assert reports[0]["summary"]["ending_equity"] > 1000
    assert reports[1]["summary"]["starting_capital"] == 1000
    assert reports[1]["summary"]["fees_paid"] == pytest.approx(reports[1]["fills"][0]["fee"])
    assert reports[1]["summary"]["realized_net_pnl"] == 0


def test_final_pending_orders_expire_as_end_of_window():
    data = dataset()
    report = run_window(data, config(data), window_spec("tail", data.sessions[49], data.sessions[49]))
    assert not report["fills"]
    assert {order["reason"] for order in report["orders"]} == {ExpirationReason.END_OF_WINDOW.value}
    assert report["summary"]["expired_order_count"] == 2


def test_missing_first_open_cannot_import_a_prior_intent():
    data = dataset(missing={"A": [49]})
    report = run_window(data, config(data), window_spec("missing", data.sessions[49], data.sessions[51]))
    assert report["decisions"][0]["reason"] == "missing_close_bar"
    assert not any(fill["at"]["session"] == data.sessions[49].isoformat() for fill in report["fills"])


def test_future_price_or_valuation_changes_do_not_change_earlier_actions():
    base = dataset()
    changed = dataset(changes={('A', 55): (500, 500)}, values=[value(), value('B'),
        value(filed_date=base.sessions[54], effective_available_date=base.sessions[55], fair_value=1.)])
    spec = window_spec("prefix", base.sessions[49], base.sessions[52])
    left = run_window(base, config(base), spec)
    right = run_window(changed, config(changed), spec)
    assert left["decisions"] == right["decisions"]
    assert left["fills"] == right["fills"]
    assert left["orders"] == right["orders"]
    assert left["portfolio_history"] == right["portfolio_history"]


def test_prefix_features_are_preserved_but_pre_window_trades_are_absent():
    data = dataset()
    report = run_window(data, config(data), window_spec("prefix", data.sessions[50], data.sessions[52]))
    assert report["decisions"][0]["action"] == "buy"
    assert report["decisions"][0]["at"]["session"] == data.sessions[50].isoformat()
    assert all(d["at"]["session"] >= data.sessions[50].isoformat() for d in report["decisions"])


def test_strict_and_legacy_cannot_be_mixed():
    data = dataset(mode=ValuationMode.LEGACY)
    spec = window_spec("mode", data.sessions[49], data.sessions[51])
    with pytest.raises(ValueError, match="policies differ"):
        run_window(data, PrimarySimulationConfig(1000, 1), spec)


def test_input_order_does_not_mutate_specs_or_results():
    data = dataset()
    specs = [window_spec("b", data.sessions[52], data.sessions[55]),
             window_spec("a", data.sessions[49], data.sessions[51])]
    before = tuple(specs)
    one = run_primary_windows(data, config(data), specs)
    two = run_primary_windows(data, config(data), reversed(specs))
    assert tuple(specs) == before
    assert one == two


def test_architecture_default_chronological_segmentation_is_nonoverlapping():
    data = dataset()
    windows = chronological_windows(data, 4)
    assert [window.window_id for window in windows] == [
        "chronological_01", "chronological_02", "chronological_03", "chronological_04"]
    covered = [day for window in windows for day in data.sessions
               if window.start <= day <= window.end]
    assert covered == list(data.sessions)
    assert all(left.end < right.start for left, right in zip(windows, windows[1:]))
    assert len(chronological_windows(data, 100)) == len(data.sessions)

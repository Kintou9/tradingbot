"""Aggregate arithmetic, overlap disclosure and deterministic fingerprints."""
import json
import math
import statistics

import pytest

from backtesting.primary_window_reporting import build_multi_window_report, serialize_multi_window_report
from backtesting.primary_windows import window_spec
from backtesting.primary_state import PrimarySimulationConfig
from tests.test_primary_strategy import dataset


def config(data, allocation=1):
    return PrimarySimulationConfig(1000, allocation, valuation_mode=data.valuation_mode)


def specs(data):
    return [window_spec("loss", data.sessions[49], data.sessions[51]),
            window_spec("gain", data.sessions[52], data.sessions[55])]


def test_hand_calculated_two_window_aggregate():
    data = dataset(symbols=('A',), changes={
        ('A', 50): (100, 80), ('A', 51): (70, 80),
        ('A', 53): (100, 125), ('A', 54): (120, 120),
    })
    report = build_multi_window_report(data, config(data), specs(data))
    by_id = {window["window"]["window_id"]: window for window in report["windows"]}
    assert by_id["loss"]["summary"]["portfolio_return_pct"] == pytest.approx(-30)
    assert by_id["gain"]["summary"]["portfolio_return_pct"] == pytest.approx(20)
    aggregate = report["aggregate_statistics"]
    assert aggregate["mean_window_return_pct"] == pytest.approx(-5)
    assert aggregate["median_window_return_pct"] == pytest.approx(-5)
    assert aggregate["minimum_window_return_pct"] == pytest.approx(-30)
    assert aggregate["maximum_window_return_pct"] == pytest.approx(20)
    assert aggregate["profitable_window_count"] == 1
    assert aggregate["losing_window_count"] == 1
    assert aggregate["flat_window_count"] == 0
    assert aggregate["total_closed_trades"] == 2
    assert report["completed_window_count"] == 2 and report["failed_window_count"] == 0
    assert "compound" not in aggregate and "sharpe" not in aggregate


def test_zero_trade_and_open_position_windows_are_valid():
    data = dataset(values=[])
    report = build_multi_window_report(data, config(data), [window_spec("zero", data.sessions[0], data.sessions[10])])
    assert report["aggregate_statistics"]["zero_trade_window_count"] == 1
    assert report["windows"][0]["summary"]["ending_equity"] == 1000
    active = dataset(symbols=('A',))
    other = build_multi_window_report(active, config(active), [window_spec("open", active.sessions[49], active.sessions[50])])
    assert other["windows"][0]["summary"]["open_position_count"] == 1
    assert other["windows"][0]["summary"]["unrealized_net_pnl"] == 0


def test_overlapping_windows_disclosed_not_independent():
    data = dataset()
    windows = [window_spec("one", data.sessions[49], data.sessions[53]),
               window_spec("two", data.sessions[52], data.sessions[55])]
    report = build_multi_window_report(data, config(data), windows)
    assert report["overlap"]["detected"] is True
    assert report["overlap"]["pairs"] == [["one", "two"]]
    assert report["overlap"]["independent_evidence"] is False
    assert "not independent" in report["overlap"]["warning"]


def test_nonoverlap_and_unequal_lengths_are_disclosed():
    data = dataset()
    report = build_multi_window_report(data, config(data), specs(data))
    assert report["overlap"]["detected"] is False
    assert report["comparability"]["compatible"] is True
    assert report["comparability"]["window_lengths_equal"] is False
    assert report["comparability"]["observed_session_counts"] == {"loss": 3, "gain": 4}


def test_input_order_byte_determinism_and_fingerprint():
    data = dataset()
    ordered = specs(data)
    one = build_multi_window_report(data, config(data), ordered)
    two = build_multi_window_report(data, config(data), reversed(ordered))
    assert one == two
    assert serialize_multi_window_report(one) == serialize_multi_window_report(two)
    decoded = json.loads(serialize_multi_window_report(one))
    assert decoded["aggregate_fingerprint"] == one["aggregate_fingerprint"]
    assert decoded["window_report_fingerprints"] == [w["report_fingerprint"] for w in decoded["windows"]]


def test_material_window_and_prepared_data_change_fingerprints():
    data = dataset()
    original = build_multi_window_report(data, config(data), specs(data))
    altered_windows = [window_spec("loss", data.sessions[49], data.sessions[50]), specs(data)[1]]
    altered = build_multi_window_report(data, config(data), altered_windows)
    assert original["aggregate_fingerprint"] != altered["aggregate_fingerprint"]
    assert original["window_report_fingerprints"][0] != altered["window_report_fingerprints"][0]
    changed_data = dataset(changes={('A', 55): (101, 101)})
    changed = build_multi_window_report(changed_data, config(changed_data), specs(changed_data))
    assert original["aggregate_fingerprint"] != changed["aggregate_fingerprint"]
    assert original["window_report_fingerprints"] != changed["window_report_fingerprints"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_aggregate_serialization_rejected(value):
    with pytest.raises(ValueError):
        serialize_multi_window_report({"schema_version": "primary_multi_window_v1",
                                       "aggregate_statistics": {"mean": value},
                                       "aggregate_fingerprint": "0" * 64})


def test_independent_arithmetic_matches_window_summaries():
    data = dataset()
    report = build_multi_window_report(data, config(data, .6), specs(data))
    returns = [window["summary"]["portfolio_return_pct"] for window in report["windows"]]
    drawdowns = [window["summary"]["max_drawdown_pct"] for window in report["windows"]]
    assert report["aggregate_statistics"]["mean_window_return_pct"] == statistics.mean(returns)
    assert report["aggregate_statistics"]["median_max_drawdown_pct"] == statistics.median(drawdowns)
    for window in report["windows"]:
        summary = window["summary"]
        assert summary["ending_equity"] - summary["starting_capital"] == pytest.approx(summary["net_pnl"])
        assert summary["gross_pnl"] - summary["fees_paid"] - summary["slippage_costs"] == pytest.approx(summary["net_pnl"])

"""Deterministic descriptive reporting for independent historical windows."""
import statistics

from backtesting.primary_inputs import canonical_json, canonical_value
from backtesting.primary_reporting import ReportingError, fingerprint
from backtesting.primary_windows import validate_windows, run_primary_windows


def overlap_pairs(specs):
    ordered = sorted(specs, key=lambda spec: (spec.start, spec.end, spec.window_id))
    return [[left.window_id, right.window_id] for index, left in enumerate(ordered)
            for right in ordered[index + 1:] if max(left.start, right.start) <= min(left.end, right.end)]


def build_multi_window_report(data, config, windows):
    specs = validate_windows(data, windows)
    reports = run_primary_windows(data, config, specs)
    overlaps = overlap_pairs(specs)
    returns = [report["summary"]["portfolio_return_pct"] for report in reports]
    drawdowns = [report["summary"]["max_drawdown_pct"] for report in reports]
    comparable = all(
        report["config"] == reports[0]["config"] and
        report["window"]["symbols"] == reports[0]["window"]["symbols"] and
        report["window"]["parent_dataset_hash"] == data.content_hash
        for report in reports
    )
    if not comparable:
        raise ValueError("Window reports do not share configuration, universe and parent data")
    statistics_report = {
        "mean_window_return_pct": statistics.mean(returns),
        "median_window_return_pct": statistics.median(returns),
        "minimum_window_return_pct": min(returns),
        "maximum_window_return_pct": max(returns),
        "median_max_drawdown_pct": statistics.median(drawdowns),
        "profitable_window_count": sum(value > 0 for value in returns),
        "losing_window_count": sum(value < 0 for value in returns),
        "flat_window_count": sum(value == 0 for value in returns),
        "total_closed_trades": sum(report["summary"]["closed_trade_count"] for report in reports),
        "zero_trade_window_count": sum(report["summary"]["fill_count"] == 0 for report in reports),
    }
    report = {
        "schema_version": "primary_multi_window_v1",
        "engine": "primary_execution_coupled_independent_windows",
        "evaluation_config": config,
        "dataset_hash": data.content_hash,
        "symbols": list(data.symbols),
        "valuation_mode": config.valuation_mode,
        "requested_window_count": len(specs),
        "completed_window_count": len(reports),
        "failed_window_count": 0,
        "windows": list(reports),
        "window_definitions": [report["window"] for report in reports],
        "window_report_fingerprints": [report["report_fingerprint"] for report in reports],
        "aggregate_statistics": statistics_report,
        "comparability": {
            "compatible": comparable,
            "same_initial_cash": True,
            "same_allocation_and_costs": True,
            "same_valuation_mode": True,
            "same_symbol_universe": True,
            "same_parent_dataset": True,
            "window_lengths_equal": len({r["window"]["tradable_session_count"] for r in reports}) == 1,
            "observed_session_counts": {r["window"]["window_id"]: r["window"]["tradable_session_count"] for r in reports},
        },
        "overlap": {
            "detected": bool(overlaps),
            "pairs": overlaps,
            "independent_evidence": not bool(overlaps),
            "warning": "Overlapping windows are sensitivity checks and are not independent observations" if overlaps else None,
        },
        "limitations": [
            "Statistics describe independent flat-start runs; returns are not compounded or an investable continuous history.",
            "Per-window Sharpe ratios are not averaged or pooled.",
            "Observed-union calendars are not exchange-calendar certification.",
            "Strict provenance validation does not certify all source facts or reconstructed model knowledge.",
            "Full-journal ledger validation has known quadratic scaling.",
        ],
    }
    report = canonical_value(report)
    report["aggregate_fingerprint"] = fingerprint(report)
    return report


def serialize_multi_window_report(report):
    body = dict(report)
    aggregate = body.pop("aggregate_fingerprint", None)
    if aggregate != fingerprint(body):
        raise ReportingError("Aggregate fingerprint mismatch")
    return canonical_json(report) + "\n"

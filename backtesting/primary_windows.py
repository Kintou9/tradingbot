"""Independent flat-start historical windows over immutable prepared features."""
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import re

from backtesting.primary_inputs import canonical_json
from backtesting.primary_reporting import summarize_primary
from backtesting.primary_state import (
    ExpirationReason, PreparedFrame, PreparedPrimaryData, PrimarySimulationConfig,
    session_date,
)
from backtesting.primary_strategy import CloseInputs, evaluate_close
from backtesting.primary_ledger import PrimaryLedger
from backtesting.metrics import split_into_windows


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    start: date
    end: date

    def __post_init__(self):
        if not isinstance(self.window_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.window_id):
            raise ValueError("Window ID must be 1-64 stable ASCII identifier characters")
        session_date(self.start)
        session_date(self.end)
        if self.end < self.start:
            raise ValueError("Window end precedes start")


def window_spec(window_id, start, end):
    """Build an immutable spec from ISO dates or date values; no date inference."""
    def parse(value, name):
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"Window {name} must be an ISO date") from exc
        if not isinstance(value, date):
            raise ValueError(f"Window {name} must be a date")
        return value
    return WindowSpec(window_id, parse(start, "start"), parse(end, "end"))


def prepare_window(data: PreparedPrimaryData, spec: WindowSpec) -> PreparedPrimaryData:
    """Select inclusive tradable sessions while retaining precomputed warm-up state.

    Features in `data` were computed from the complete historical prefix. Rows
    before start are not passed to the ledger and cannot create decisions/fills.
    """
    if spec.start not in data.sessions:
        raise ValueError(f"Window {spec.window_id} start is not an observed session")
    if spec.end not in data.sessions:
        raise ValueError(f"Window {spec.window_id} end is not an observed session")
    sessions = tuple(d for d in data.sessions if spec.start <= d <= spec.end)
    if not sessions:
        raise ValueError(f"Window {spec.window_id} contains no observed sessions")
    frames = []
    for frame in data.frames:
        selected = [(d, row) for d, row in zip(frame.dates, frame.rows) if spec.start <= d <= spec.end]
        frames.append(PreparedFrame(frame.symbol, tuple(d for d, _ in selected), frame.columns,
                                    tuple(row for _, row in selected)))
    metadata = data.metadata
    metadata["parent_dataset_hash"] = data.content_hash
    metadata["window_id"] = spec.window_id
    metadata["window_start"] = spec.start.isoformat()
    metadata["window_end"] = spec.end.isoformat()
    metadata["window_policy"] = "inclusive_observed_sessions; prior_rows_feature_warmup_only"
    quality = data.data_quality
    quality["window_id"] = spec.window_id
    quality["warmup_observation_count"] = sum(d < spec.start for d in data.sessions)
    quality["missing_sessions"] = {
        symbol: [d for d in dates if spec.start.isoformat() <= d <= spec.end.isoformat()]
        for symbol, dates in quality["missing_sessions"].items()
    }
    valuations = tuple(value for value in data.valuations
                       if value.observation.effective_available_date <= spec.end)
    payload = dict(parent_dataset_hash=data.content_hash, spec=spec, sessions=sessions,
                   frames=frames, valuations=valuations, metadata=metadata, quality=quality)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return PreparedPrimaryData(data.symbols, sessions, tuple(frames), valuations,
                               data.valuation_mode, canonical_json(metadata),
                               canonical_json(quality), digest)


def run_window(data: PreparedPrimaryData, config: PrimarySimulationConfig, spec: WindowSpec):
    """Run one fresh ledger and return its complete Step 4-compatible report."""
    window_data = prepare_window(data, spec)
    ledger = PrimaryLedger(window_data, config)
    inputs = CloseInputs(window_data)
    decisions = []
    for _ in window_data.sessions:
        ledger.open_session()
        view = ledger.close_session()
        symbols = sorted(window_data.symbols, key=lambda symbol: (symbol not in view.positions, symbol))
        batch = [evaluate_close(symbol, inputs.at(symbol, view.at), view, config) for symbol in symbols]
        ledger.submit_close(d.intent for d in batch if d.intent is not None)
        decisions.extend(batch)
    result = replace(ledger.finish(ExpirationReason.END_OF_WINDOW), decisions=tuple(decisions))
    report = summarize_primary(result, window_data)
    report["window"] = {
        "window_id": spec.window_id,
        "requested_start": spec.start.isoformat(),
        "requested_end": spec.end.isoformat(),
        "effective_start": window_data.sessions[0].isoformat(),
        "effective_end": window_data.sessions[-1].isoformat(),
        "boundary_policy": "inclusive; exact observed sessions required",
        "warmup_observation_count": sum(d < spec.start for d in data.sessions),
        "tradable_session_count": len(window_data.sessions),
        "symbols": list(window_data.symbols),
        "initial_cash": config.initial_cash,
        "allocation_pct": config.allocation_pct,
        "valuation_mode": config.valuation_mode.value,
        "parent_dataset_hash": data.content_hash,
        "window_dataset_hash": window_data.content_hash,
    }
    from backtesting.primary_reporting import fingerprint
    report["report_fingerprint"] = fingerprint(report)
    return report


def validate_windows(data: PreparedPrimaryData, windows):
    specs = tuple(windows)
    if not specs:
        raise ValueError("At least one explicit window is required")
    if any(not isinstance(spec, WindowSpec) for spec in specs):
        raise ValueError("Windows require typed WindowSpec values")
    ids = [spec.window_id for spec in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate window ID")
    ordered = tuple(sorted(specs, key=lambda spec: (spec.start, spec.end, spec.window_id)))
    for spec in ordered:
        prepare_window(data, spec)
    return ordered


def chronological_windows(data: PreparedPrimaryData, n_windows=4):
    """Architecture-defined nonoverlapping default segmentation, oldest first."""
    import pandas as pd
    slices = split_into_windows(pd.Series(data.sessions), n_windows=n_windows)
    nonempty = [part for part in slices if not part.empty]
    width = max(2, len(str(len(nonempty))))
    return tuple(WindowSpec(f"chronological_{index:0{width}d}", part.iloc[0], part.iloc[-1])
                 for index, part in enumerate(nonempty, start=1))


def run_primary_windows(data: PreparedPrimaryData, config: PrimarySimulationConfig, windows):
    """Fail atomically: any invalid/failed window prevents an aggregate result."""
    specs = validate_windows(data, windows)
    return tuple(run_window(data, config, spec) for spec in specs)

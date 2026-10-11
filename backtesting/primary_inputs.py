"""Deterministic offline input preparation; never fetches or generates research."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
from importlib.metadata import version
from collections.abc import Mapping

import numpy as np
import pandas as pd

from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
from backtesting.primary_state import (
    PreparedFrame, PreparedPrimaryData, PreparedValuation, ProvenanceStatus,
    ValuationMode, ValuationObservation, symbol_name,
)


POLICY_VERSION = "primary_preparation_v1"
PRICE_COLUMNS = ("open", "high", "low", "close", "volume")


def canonical_value(value):
    if is_dataclass(value):
        return canonical_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Ambiguous naive metadata timestamp")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("Metadata keys must be strings")
        return {k: canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [canonical_value(v) for v in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and np.isfinite(value):
        return value
    raise ValueError("Only finite canonical JSON metadata is supported")


def canonical_json(value):
    return json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize_sessions(values, name):
    dates, awareness = [], set()
    for value in values:
        if isinstance(value, str):
            # Accept unambiguous ISO dates/timestamps only, never inferred US/EU formats.
            try:
                parsed = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{name}: use ISO session dates") from exc
        else:
            parsed = value
        if not isinstance(parsed, (date, datetime, pd.Timestamp)) or pd.isna(parsed):
            raise ValueError(f"{name}: invalid date")
        stamp = pd.Timestamp(parsed)
        awareness.add(stamp.tzinfo is not None)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("America/New_York")
        if stamp != stamp.normalize():
            raise ValueError(f"{name}: intraday/ambiguous date label; require exchange midnight")
        dates.append(stamp.date())
    if len(awareness) > 1:
        raise ValueError(f"{name}: mixed timezone-aware and naive dates")
    if not dates or dates != sorted(set(dates)):
        raise ValueError(f"{name}: sessions must be nonempty, sorted and unique")
    return tuple(dates)


def prepare_valuation(observation, mode):
    """Eligibility is distinct from provenance; every observation remains visible."""
    if observation.fair_value is None:
        return PreparedValuation(observation, False, ProvenanceStatus.UNVERIFIED, observation.invalidation_reason)
    if observation.share_count_source != "filing":
        return PreparedValuation(observation, False, ProvenanceStatus.UNVERIFIED, "share_count_not_filing")
    if mode == ValuationMode.LEGACY:
        return PreparedValuation(observation, True, ProvenanceStatus.UNVERIFIED, "legacy_point_in_time_unverified")
    evidence = observation.fact_evidence
    verified = (
        bool(observation.required_fact_ids) and
        set(observation.required_fact_ids) == {e.fact_id for e in evidence} and
        all(e.available_date <= observation.filed_date and
            e.context_hash == observation.filing_context_hash for e in evidence) and
        all((observation.filing_context_hash, observation.raw_output_hash,
             observation.model_version, observation.prompt_version, observation.generated_at))
    )
    return PreparedValuation(observation, bool(verified),
        ProvenanceStatus.VERIFIED if verified else ProvenanceStatus.UNVERIFIED,
        None if verified else "per_fact_availability_unverified")


def prepare_primary_data(prices_by_symbol, valuation_records, *, session_index, metadata):
    """Prepare immutable captured inputs; metadata selects provenance mode explicitly.

    Requires metadata calendar_source, price_source and adjustment_policy. Short
    histories (<35 bars) cannot pass the SMA50 entry warmup; avoid the existing
    all-indicator helper's insufficient-MACD-history failure and leave SMA50 NaN.
    No padding, fabricated bars, or alternate indicator formulas are used.
    """
    if not isinstance(prices_by_symbol, Mapping) or not prices_by_symbol:
        raise ValueError("A nonempty symbol mapping is required")
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    meta = json.loads(canonical_json(metadata))  # reject objects; deep defensive copy
    for key in ("calendar_source", "price_source", "adjustment_policy"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise ValueError(f"Missing input metadata: {key}")
    if meta["calendar_source"] not in {"observed_union", "archived_exchange_calendar"}:
        raise ValueError("Unsupported calendar source")
    mode = ValuationMode(meta.get("valuation_mode", ValuationMode.VERIFIED.value))
    meta["valuation_mode"] = mode.value
    if isinstance(valuation_records, (pd.Series, Mapping)):
        raise ValueError("Valuations require typed observations, not numeric series/mappings")
    sessions = normalize_sessions(session_index, "canonical calendar")
    universe = {}
    for raw_symbol, frame in prices_by_symbol.items():
        symbol = symbol_name(raw_symbol)
        if symbol in universe:
            raise ValueError("Symbol normalization collision")
        universe[symbol] = frame
    frames, missing, short_history = [], {}, []
    for symbol in sorted(universe):
        frame = universe[symbol]
        if not isinstance(frame, pd.DataFrame) or set(frame.columns) != set(PRICE_COLUMNS) or len(frame.columns) != 5:
            raise ValueError(f"{symbol}: require exactly OHLCV columns")
        dates = normalize_sessions(frame.index, symbol)
        if not set(dates) <= set(sessions):
            raise ValueError(f"{symbol}: bars outside canonical calendar")
        if any(not pd.api.types.is_numeric_dtype(frame[c]) or pd.api.types.is_bool_dtype(frame[c]) or
               pd.api.types.is_complex_dtype(frame[c]) for c in PRICE_COLUMNS):
            raise ValueError(f"{symbol}: OHLCV must be numeric")
        data = frame.loc[:, list(PRICE_COLUMNS)].astype(float).copy(deep=True)
        data.index = pd.DatetimeIndex(dates)
        values = data.to_numpy()
        if not np.isfinite(values).all() or (data[list(PRICE_COLUMNS[:4])] <= 0).any().any() or (data.volume < 0).any():
            raise ValueError(f"{symbol}: invalid price/volume")
        if ((data.high < data[["open", "low", "close"]].max(axis=1)) |
                (data.low > data[["open", "high", "close"]].min(axis=1))).any():
            raise ValueError(f"{symbol}: inconsistent OHLC geometry")
        missing[symbol] = [d.isoformat() for d in sessions if d not in set(dates)]
        if len(data) < 35:
            short_history.append(symbol)
            data["sma_50"] = np.nan
        else:
            data = compute_indicators(data)
        data = data.join(rolling_support_resistance(data))
        columns = tuple(sorted(data.columns))
        rows = tuple(tuple(None if pd.isna(v) else float(v) for v in row)
                     for row in data.loc[:, list(columns)].itertuples(index=False, name=None))
        if any(v is not None and not np.isfinite(v) for row in rows for v in row):
            raise ValueError(f"{symbol}: nonfinite derived indicator")
        frames.append(PreparedFrame(symbol, dates, columns, rows))
    seen = {}
    for observation in valuation_records:
        if not isinstance(observation, ValuationObservation) or observation.symbol not in universe:
            raise ValueError("Unknown symbol or untyped valuation record")
        key = (observation.symbol, observation.effective_available_date)
        if key in seen and seen[key] != observation:
            raise ValueError("Conflicting valuation observations at one availability boundary")
        seen[key] = observation  # identical duplicates collapse deterministically
    valuations = tuple(prepare_valuation(seen[k], mode) for k in sorted(seen))
    for symbol in universe:
        ordered = [v.observation.filed_date for v in valuations if v.observation.symbol == symbol]
        if ordered != sorted(ordered):
            raise ValueError("Filing chronology conflicts with availability chronology")
    meta["preparation_policy"] = POLICY_VERSION
    meta["indicator_dependencies"] = {name: version(name) for name in ("pandas", "numpy", "pandas-ta")}
    quality = {"missing_sessions": missing, "short_indicator_history": short_history,
        "missing_valuation_symbols": sorted(set(universe)-{v.observation.symbol for v in valuations}),
        "ineligible_valuations": sum(not v.eligible for v in valuations),
        "strict_point_in_time_certified": False,
        "knowledge_contamination_status": "unverified",
        "calendar_complete": meta["calendar_source"] == "archived_exchange_calendar"}
    payload = {"symbols": sorted(universe), "sessions": sessions, "frames": frames,
               "valuations": valuations, "metadata": meta, "data_quality": quality}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return PreparedPrimaryData(tuple(sorted(universe)), sessions, tuple(frames), valuations, mode,
                               canonical_json(meta), canonical_json(quality), digest)

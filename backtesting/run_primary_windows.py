"""Offline explicit-window CLI using the Step 4 prepared-data manifest."""
import argparse
import json
import os
from pathlib import Path

from backtesting.primary_state import PrimarySimulationConfig, ValuationMode
from backtesting.primary_window_reporting import build_multi_window_report, serialize_multi_window_report
from backtesting.primary_windows import window_spec
from backtesting.run_primary_backtest import _json, load_prepared_manifest
from engine.entry_exit.risk import Limits


def load_window_specs(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Window specification must not be a symlink")
    payload = _json(path.read_text(encoding="utf-8"))
    if set(payload) != {"schema_version", "windows"} or payload["schema_version"] != "primary_window_spec_v1":
        raise ValueError("Window file requires only schema_version and windows")
    if not isinstance(payload["windows"], list):
        raise ValueError("windows must be a JSON list")
    specs = []
    for item in payload["windows"]:
        if not isinstance(item, dict) or set(item) != {"id", "start", "end"}:
            raise ValueError("Each window requires exactly id, start and end")
        specs.append(window_spec(item["id"], item["start"], item["end"]))
    return tuple(specs)


def _input_paths(manifest_path, windows_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = _json(manifest_path.read_text(encoding="utf-8"))
    paths = {manifest_path, Path(windows_path).resolve()}
    base = manifest_path.parent
    for value in list(manifest.get("prices", {}).values()) + [manifest.get("valuations_path")]:
        if value is not None:
            paths.add((base / value).resolve())
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline independent multi-window primary evaluation")
    parser.add_argument("--input", required=True, help="primary_prepared_input_v1 manifest")
    parser.add_argument("--windows", required=True, help="primary_window_spec_v1 JSON")
    parser.add_argument("--output", required=True)
    parser.add_argument("--valuation-mode", choices=[mode.value for mode in ValuationMode],
                        default=ValuationMode.VERIFIED.value)
    parser.add_argument("--initial-cash", type=float)
    parser.add_argument("--allocation", type=float)
    parser.add_argument("--fees", type=float)
    parser.add_argument("--slippage", type=float)
    parser.add_argument("--quantity-increment", type=float)
    args = parser.parse_args(argv)
    try:
        if Path(args.input).is_symlink():
            raise ValueError("Prepared-data manifest must not be a symlink")
        specs = load_window_specs(args.windows)
        data = load_prepared_manifest(args.input, args.valuation_mode)
        config = PrimarySimulationConfig(
            args.initial_cash if args.initial_cash is not None else float(os.getenv("STARTING_CAPITAL", "20")),
            args.allocation if args.allocation is not None else Limits.from_env().max_position_pct,
            fee_pct=args.fees if args.fees is not None else float(os.getenv("BACKTEST_FEES_PCT", "0")),
            slippage_pct=args.slippage if args.slippage is not None else float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0")),
            quantity_increment=args.quantity_increment,
            valuation_mode=ValuationMode(args.valuation_mode),
            calendar_policy=data.metadata["calendar_source"],
        )
        output = Path(args.output)
        if output.resolve() in _input_paths(args.input, args.windows):
            raise ValueError("Output must not collide with an input file")
        report = build_multi_window_report(data, config, specs)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialize_multi_window_report(report))
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

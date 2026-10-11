"""Minimal offline entrypoint: python -m backtesting.run_primary_backtest.

Manifest primary_prepared_input_v1 references hashed local CSV/JSON files.
No provider fallback, pickle, loader callbacks or environment mutation.
"""
import argparse
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path

import pandas as pd

from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_state import FactEvidence, ValuationObservation, ValuationMode, PrimarySimulationConfig, symbol_name
from backtesting.primary_strategy import run_primary_strategy
from backtesting.primary_reporting import summarize_primary, serialize_report, compare_primary_runs
from engine.entry_exit.risk import Limits


def _json(text):
    def pairs(items):
        result = {}
        for k,v in items:
            if k in result:
                raise ValueError('Duplicate JSON key')
            result[k] = v
        return result
    def reject(value):
        raise ValueError('Nonfinite JSON number: '+value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def load_prepared_manifest(path, valuation_mode=ValuationMode.VERIFIED):
    path = Path(path)
    manifest = _json(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 'primary_prepared_input_v1':
        raise ValueError('Unsupported input manifest schema')
    symbols = manifest['symbols']
    if not symbols or symbols != sorted(set(symbols)) or any(s != symbol_name(s) for s in symbols):
        raise ValueError('Manifest symbols must be normalized, sorted, unique')
    if set(manifest['prices']) != set(symbols):
        raise ValueError('Incomplete price mapping')
    base = path.resolve().parent
    hashes = manifest['hashes']
    references = list(manifest['prices'].values()) + [manifest['valuations_path']]
    if set(hashes) != set(references):
        raise ValueError('Require exactly one hash per referenced input')
    def read(reference):
        relative = Path(reference)
        target = (base/relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(base):
            raise ValueError('Input paths must stay relative to the manifest directory')
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != hashes[reference]:
            raise ValueError('Input SHA-256 mismatch')
        return content
    frames = {}
    for symbol in symbols:
        frame = pd.read_csv(io.BytesIO(read(manifest['prices'][symbol])))
        if list(frame.columns) != ['date','open','high','low','close','volume']:
            raise ValueError('CSV requires exactly date/open/high/low/close/volume')
        frames[symbol] = frame.set_index('date')
    records = _json(read(manifest['valuations_path']).decode('utf-8'))
    if not isinstance(records, list):
        raise ValueError('Valuations must be a JSON list')
    valuations = []
    for item in records:
        fields = dict(item)
        fields['filed_date'] = date.fromisoformat(fields['filed_date'])
        fields['effective_available_date'] = date.fromisoformat(fields['effective_available_date'])
        if fields.get('generated_at') is not None:
            fields['generated_at'] = datetime.fromisoformat(fields['generated_at'])
        fields['fact_evidence'] = tuple(FactEvidence(**(dict(f) | {'available_date':date.fromisoformat(f['available_date'])}))
                                         for f in fields.get('fact_evidence', []))
        valuations.append(ValuationObservation(**fields))
    metadata = dict(manifest['source_metadata'])
    metadata.update(calendar_source=manifest['calendar_source'], valuation_mode=ValuationMode(valuation_mode).value,
                    input_file_hashes={k:v for k,v in sorted(hashes.items())})
    return prepare_primary_data(frames, valuations, session_index=manifest['session_dates'], metadata=metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline primary historical simulation from hashed captured inputs')
    parser.add_argument('--input', required=True, help='primary_prepared_input_v1 JSON manifest')
    parser.add_argument('--output', required=True)
    parser.add_argument('--engine', choices=('coupled','replay','compare'), default='coupled')
    parser.add_argument('--valuation-mode', choices=[m.value for m in ValuationMode], default=ValuationMode.VERIFIED.value)
    parser.add_argument('--initial-cash', type=float)
    parser.add_argument('--allocation', type=float)
    parser.add_argument('--fees', type=float)
    parser.add_argument('--slippage', type=float)
    parser.add_argument('--quantity-increment', type=float)
    args = parser.parse_args(argv)
    try:
        if args.engine != 'coupled' and args.valuation_mode != ValuationMode.LEGACY.value:
            raise ValueError('Replay/compare requires explicit --valuation-mode legacy_research_inputs')
        data = load_prepared_manifest(args.input, args.valuation_mode)
        config = PrimarySimulationConfig(
            args.initial_cash if args.initial_cash is not None else float(os.getenv('STARTING_CAPITAL','20')),
            args.allocation if args.allocation is not None else Limits.from_env().max_position_pct,
            fee_pct=args.fees if args.fees is not None else float(os.getenv('BACKTEST_FEES_PCT','0')),
            slippage_pct=args.slippage if args.slippage is not None else float(os.getenv('BACKTEST_SLIPPAGE_PCT','0')),
            quantity_increment=args.quantity_increment, valuation_mode=ValuationMode(args.valuation_mode),
            calendar_policy=data.metadata['calendar_source'])
        if args.engine == 'coupled':
            report = summarize_primary(run_primary_strategy(data, config), data)
        else:
            from backtesting.primary_baseline import run_replay_comparison, complete_comparison_data
            if args.engine == 'replay':
                report = run_replay_comparison(data, config)
            else:
                data, manifest = complete_comparison_data(data)
                report = compare_primary_runs(run_replay_comparison(data, config), run_primary_strategy(data, config), manifest)
        # Exclusive create protects the manifest, price files, and unrelated work.
        with Path(args.output).open('x', encoding='utf-8', newline='\n') as output:
            output.write(serialize_report(report))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

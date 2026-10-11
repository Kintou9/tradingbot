"""Offline isolation of unchanged Phase 1 function bodies, not a new simulator.

Compile only named local function definitions in a private namespace. Module
imports/top-level client and dotenv initialization never execute. No global
module patching or environment mutation. Original vectorbt still owns fills.
"""
import ast
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import vectorbt as vbt
from vectorbt.portfolio.enums import SizeType

from backtesting import metrics
from backtesting.primary_inputs import canonical_value, prepare_primary_data
from backtesting.primary_state import ValuationMode
from engine.entry_exit.signals import evaluate_entry, evaluate_exit

BASE_COMMIT = 'a033771841bcb9e7f55a0ba68f05377199e51c3d'
FUNCTIONS = ('_technical_proxy', 'fair_value_asof', 'generate_signals', 'run_backtest',
             'summarize', 'run_shared_capital_backtest')


def isolated_baseline(data, config):
    """Bind approved captured inputs to original functions without client imports."""
    if data.valuation_mode != ValuationMode.LEGACY or config.valuation_mode != ValuationMode.LEGACY:
        raise ValueError('Baseline requires explicit legacy_research_inputs mode')
    source = Path(__file__).with_name('backtest_runner.py').read_bytes()
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
    if {n.name for n in nodes} != set(FUNCTIONS) or any(n.decorator_list for n in nodes):
        raise ValueError('Unsupported baseline source interface')
    frames = data.prices_by_symbol
    fair_values = {}
    for symbol in data.symbols:
        series = {}
        for prepared in data.valuations:
            observation = prepared.observation
            if observation.symbol != symbol:
                continue
            # Original Phase 1 has only a filing-date boundary. Do not silently
            # change its valuation timing to match an extended typed boundary.
            if any(observation.filed_date < d < observation.effective_available_date for d in data.sessions):
                raise ValueError('Baseline cannot represent delayed valuation availability')
            if observation.filed_date in series:
                raise ValueError('Baseline cannot represent multiple observations on one filing date')
            series[observation.filed_date] = observation.fair_value if prepared.eligible else float('nan')
        fair_values[symbol] = pd.Series(series, dtype=float).sort_index()
    captured = {}
    def from_signals(*args, **kwargs):
        portfolio = vbt.Portfolio.from_signals(*args, **kwargs)
        captured['portfolio'] = portfolio
        return portfolio
    environment = {'BACKTEST_FEES_PCT':str(config.fee_pct), 'BACKTEST_SLIPPAGE_PCT':str(config.slippage_pct)}
    namespace = dict(pd=pd, math=math, vbt=SimpleNamespace(Portfolio=SimpleNamespace(from_signals=from_signals)),
        SizeType=SizeType, os=SimpleNamespace(getenv=environment.get), STARTING_CAPITAL=config.initial_cash,
        NEUTRAL_SENTIMENT=0., evaluate_entry=evaluate_entry, evaluate_exit=evaluate_exit,
        Limits=SimpleNamespace(from_env=lambda:SimpleNamespace(max_position_pct=config.allocation_pct)),
        clean_metric=metrics.clean_metric, validate_prices=metrics.validate_prices,
        validate_economics=metrics.validate_economics,
        fetch_ohlcv=lambda ticker, outputsize=5000:frames[ticker][['open','high','low','close','volume']].copy(),
        get_point_in_time_fair_values=lambda ticker, company_name:fair_values[ticker].copy())
    # Execute original trailing feature computation on identical captured OHLCV.
    from engine.price_action_engine.indicators import compute_indicators, rolling_support_resistance
    namespace['compute_indicators'] = compute_indicators
    namespace['rolling_support_resistance'] = rolling_support_resistance
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<phase1-offline-functions>', 'exec'), namespace)
    return namespace, captured, hashlib.sha256(source).hexdigest()


def generate_replay_from_prepared(data, symbol, *, baseline_starting_capital, baseline_allocation_pct):
    from backtesting.primary_state import PrimarySimulationConfig
    config = PrimarySimulationConfig(baseline_starting_capital, baseline_allocation_pct,
        valuation_mode=ValuationMode.LEGACY, calendar_policy=data.metadata['calendar_source'])
    namespace, _, _ = isolated_baseline(data, config)
    return namespace['generate_signals'](symbol, symbol)


def _records(frame):
    """Explicit numpy-scalar conversion; undefined vectorbt statistics become null."""
    rows = []
    for row in frame.to_dict('records'):
        rows.append({k:(None if pd.isna(v) else v.item() if hasattr(v,'item') else v) for k,v in row.items()})
    return rows


def run_replay_comparison(data, config):
    from backtesting.primary_reporting import fingerprint
    namespace, captured, source_hash = isolated_baseline(data, config)
    signals = {s:namespace['generate_signals'](s,s) for s in data.symbols}
    summary = namespace['run_shared_capital_backtest'](signals, initial_cash=config.initial_cash)
    if not summary['supported']:
        raise ValueError(summary['reason'])
    portfolio = captured['portfolio']
    orders = portfolio.orders.records
    index = portfolio.wrapper.index
    symbols = sorted(data.symbols)
    fills = [dict(symbol=symbols[int(row.col)], side='buy' if int(row.side)==0 else 'sell',
        session=index[int(row.idx)].date(), quantity=float(row.size), price=float(row.price), fees=float(row.fees))
        for row in orders.itertuples()]
    values = portfolio.value()
    summary['ending_equity'] = float(values.iloc[-1])
    report = dict(schema_version='primary_signal_replay_phase1', engine='original_phase1_vectorbt',
        config=config, manifest=dict(dataset_hash=data.content_hash, baseline_commit=BASE_COMMIT,
            baseline_source_sha256=source_hash, symbols=symbols,
            start=index[0].date(), end=index[-1].date(), valuation_mode=ValuationMode.LEGACY,
            captured_environment=dict(STARTING_CAPITAL=config.initial_cash, MAX_POSITION_PCT=config.allocation_pct,
                BACKTEST_FEES_PCT=config.fee_pct, BACKTEST_SLIPPAGE_PCT=config.slippage_pct)),
        summary=summary, fills=fills, order_records=_records(orders), trade_records=_records(portfolio.trades.records),
        equity_curve=[dict(session=d.date(), equity=float(v)) for d,v in values.items()],
        cash_curve=[dict(session=d.date(), cash=float(v)) for d,v in portfolio.cash().items()],
        signals={s:dict(entries=[d.date() for d,v in entry.items() if v], exits=[d.date() for d,v in exit_.items() if v])
                 for s,(_,entry,exit_) in signals.items()},
        limitations=['historically unverified', 'Precomputed assumed position state is intentionally retained.',
            'No rejected-order journal is exposed by the original baseline.',
            'Quantity increments and $1 execution minimum are not baseline controls.',
            'Baseline num_trades includes open trades; do not equate it with fills.'])
    report = canonical_value(report)
    report['report_fingerprint'] = fingerprint(report)
    return report


def complete_comparison_data(data):
    """Explicit filtered comparison slice, not validation of missing sessions."""
    frames = data.prices_by_symbol
    common = set(data.sessions)
    for frame in frames.values():
        common &= set(frame.index.date)
    sessions = sorted(common)
    if len(sessions)<2:
        raise ValueError('Comparison needs at least two common complete sessions')
    raw = {s:f.loc[pd.DatetimeIndex(sessions), ['open','high','low','close','volume']] for s,f in frames.items()}
    meta = data.metadata
    meta['comparison_slice'] = 'complete sessions; features recomputed on identical filtered data'
    prepared = prepare_primary_data(raw, [v.observation for v in data.valuations], session_index=sessions, metadata=meta)
    return prepared, dict(original_dataset_hash=data.content_hash, comparison_dataset_hash=prepared.content_hash,
        excluded_sessions=[d.isoformat() for d in data.sessions if d not in common],
        policy='Common complete-session slice; not missing-session validation; features recomputed for both engines')

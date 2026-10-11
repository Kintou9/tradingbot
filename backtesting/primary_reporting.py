"""Versioned historical reports; reconcile accepted economics, never simulate fills."""
from collections import Counter
import hashlib
from importlib.metadata import version
import math
from pathlib import Path
import statistics

from backtesting.primary_inputs import canonical_json, canonical_value
from backtesting.primary_state import Phase, Side, OrderStatus, MarkQuality


class ReportingError(ValueError):
    pass


def fingerprint(report):
    """SHA-256 of canonical UTF-8 JSON, excluding its own fingerprint field."""
    body = {k: v for k, v in report.items() if k != 'report_fingerprint'}
    return hashlib.sha256(canonical_json(body).encode('utf-8')).hexdigest()


def serialize_report(report):
    body = dict(report)
    digest = fingerprint(body)
    if 'report_fingerprint' in body and body['report_fingerprint'] != digest:
        raise ReportingError('Report fingerprint mismatch')
    body['report_fingerprint'] = digest
    return canonical_json(body) + '\n'


def summarize_primary(result, data=None):
    """Daily closes, sample-session Sharpe, closed-only outcomes, no liquidation.

    Absolute reconciliation tolerance is 1e-8 * max(1, starting capital).
    Unknown/source metadata is not echoed: it may contain local paths or secrets.
    """
    cfg = result.config
    tolerance = 1e-8 * max(1., cfg.initial_cash)
    def equal(a, b, label):
        if not math.isfinite(a) or not math.isfinite(b) or not math.isclose(a, b, rel_tol=0, abs_tol=tolerance):
            raise ReportingError('Reconciliation failed: ' + label)
    def require(condition, label):
        if not condition:
            raise ReportingError(label)
    require(bool(result.history), 'Report requires completed history')
    if data is not None:
        require(data.content_hash == result.dataset_hash and data.valuation_mode == cfg.valuation_mode,
                'Prepared input identity differs')
    fills = result.fills
    require(list(fills) == sorted(fills, key=lambda f: f.at), 'Fills not chronological')
    require(tuple(o.fill for o in result.outcomes if o.status == OrderStatus.FILLED) == fills,
            'Accepted outcomes differ from fills')
    intents = [d.intent for d in result.decisions if d.intent is not None]
    intent_map = {i.order_id: i for i in intents}
    require(len(intent_map) == len(intents), 'Duplicate submitted intent')
    require(set(intent_map) == {o.order_id for o in result.outcomes}, 'Missing terminal intent/outcome')
    for outcome in result.outcomes:
        intent = intent_map[outcome.order_id]
        require(intent.signal_at <= outcome.at <= result.history[-1].at, 'Outcome outside completed history')
        if outcome.fill:
            f = outcome.fill
            require((f.symbol, f.side, f.signal_at, f.at.session) ==
                    (intent.symbol, intent.side, intent.signal_at, intent.eligible_session), 'Fill/intent mismatch')
        else:
            equal(outcome.cash_delta, 0., 'Nonfill cash delta')
    cash, fees, slip, realized, realized_gross = cfg.initial_cash, 0., 0., 0., 0.
    holdings, closed, daily = {}, [], {}
    cursor = 0
    for snap in result.history:
        while cursor < len(fills) and fills[cursor].at <= snap.at:
            f = fills[cursor]
            if f.side == Side.BUY:
                require(f.symbol not in holdings, 'Duplicate accepted holding')
                equal(f.cash_delta, -(f.quantity*f.price+f.fee), 'Buy debit')
                holdings[f.symbol] = f
            else:
                require(f.symbol in holdings, 'Exit without accepted entry')
                entry = holdings.pop(f.symbol)
                equal(f.quantity, entry.quantity, 'Full exit quantity')
                equal(f.cash_delta, f.quantity*f.price-f.fee, 'Sale credit')
                net = f.cash_delta + entry.cash_delta
                gross = f.quantity*(f.reference_price-entry.reference_price)
                realized += net
                realized_gross += gross
                closed.append(dict(symbol=f.symbol, entry_order_id=entry.order_id, exit_order_id=f.order_id,
                    entry_session=entry.at.session, exit_session=f.at.session, quantity=f.quantity,
                    entry_cost_basis=-entry.cash_delta, realized_gross_pnl=gross, realized_net_pnl=net,
                    return_pct=100*net/(-entry.cash_delta)))
            cash += f.cash_delta
            fees += f.fee
            slip += f.slippage_cost
            require(cash >= -tolerance, 'Negative reconstructed cash')
            cursor += 1
        marks = dict(snap.marks)
        require(set(marks) == set(holdings), 'Marked holdings differ from fills')
        exposure, unrealized, unrealized_gross = 0., 0., 0.
        for symbol, entry in holdings.items():
            mark = marks[symbol]
            require(mark.value is not None, 'Missing held mark')
            exposure += entry.quantity*mark.value
            unrealized += entry.quantity*mark.value+entry.cash_delta
            unrealized_gross += entry.quantity*(mark.value-entry.reference_price)
        equal(cash, snap.cash, 'Snapshot cash')
        equal(exposure, snap.exposure, 'Snapshot exposure')
        equal(cash+exposure, snap.equity, 'Snapshot equity')
        equal(realized+unrealized, snap.equity-cfg.initial_cash, 'Net P&L')
        equal(realized_gross+unrealized_gross-fees-slip, realized+unrealized, 'Gross less costs')
        if snap.at.phase == Phase.CLOSE:
            daily[snap.at.session] = dict(session=snap.at.session, cash=cash, equity=snap.equity,
                marked_position_value=exposure, gross_exposure=exposure,
                exposure_fraction=exposure/snap.equity if snap.equity else None,
                realized_net_pnl=realized, unrealized_net_pnl=unrealized,
                realized_gross_pnl=realized_gross, unrealized_gross_pnl=unrealized_gross,
                cumulative_fees=fees, cumulative_slippage_costs=slip,
                position_count=len(holdings), stale_symbols=sorted(s for s,m in marks.items() if m.quality != MarkQuality.CURRENT),
                marks=marks)
    require(cursor == len(fills), 'Fill occurs beyond history')
    require(bool(daily), 'No closing snapshots')
    require(result.history[-1].at.phase == Phase.CLOSE, 'Run must end at close')
    if data is not None:
        require(tuple(daily) == data.sessions, 'Daily report does not cover prepared sessions')
    require(len(result.closed_trades) == len(closed), 'Closed-trade count differs')
    fill_map = {f.order_id: f for f in fills}
    for trade, record in zip(result.closed_trades, closed):
        require(trade.entry == fill_map[record['entry_order_id']] and trade.exit == fill_map[record['exit_order_id']],
                'Closed-trade journal differs')
    terminal = {p.symbol:p for p in result.terminal_positions}
    require(set(terminal) == set(holdings), 'Terminal positions differ')
    for symbol, entry in holdings.items():
        p = terminal[symbol]
        equal(p.quantity, entry.quantity, 'Terminal quantity')
        equal(p.cost_basis, -entry.cash_delta, 'Terminal basis')
        require(p.entry_order_id == entry.order_id and p.mark == dict(result.history[-1].marks)[symbol],
                'Terminal mark/entry differs')
    curve = list(daily.values())
    ending = curve[-1]
    peak, dd, previous, returns = cfg.initial_cash, 0., cfg.initial_cash, []
    for point in curve:
        equity = point['equity']
        peak = max(peak, equity)
        dd = max(dd, (peak-equity)/peak)
        returns.append(equity/previous-1 if previous > 0 else None)
        previous = equity
    reason = None
    sharpe = None
    if len(returns) < 2:
        reason = 'fewer_than_two_returns'
    elif any(r is None or not math.isfinite(r) for r in returns):
        reason = 'invalid_return_denominator'
    elif statistics.stdev(returns) == 0:
        reason = 'zero_variance'
    else:
        sharpe = statistics.mean(returns)/statistics.stdev(returns)*math.sqrt(252)
        if not math.isfinite(sharpe):
            sharpe, reason = None, 'nonfinite_result'
    pnls = [t['realized_net_pnl'] for t in closed]
    losses = -sum(x for x in pnls if x < 0)
    outcomes = Counter(o.status.value for o in result.outcomes)
    summary = dict(starting_capital=cfg.initial_cash, ending_cash=ending['cash'], ending_equity=ending['equity'],
        portfolio_return_pct=100*(ending['equity']/cfg.initial_cash-1),
        realized_net_pnl=realized, unrealized_net_pnl=ending['unrealized_net_pnl'],
        realized_gross_pnl=realized_gross, unrealized_gross_pnl=ending['unrealized_gross_pnl'],
        gross_pnl=realized_gross+ending['unrealized_gross_pnl'], net_pnl=ending['equity']-cfg.initial_cash,
        fees_paid=fees, slippage_costs=slip, max_drawdown_pct=100*dd,
        closed_trade_count=len(closed), open_position_count=len(terminal), fill_count=len(fills),
        buy_fill_count=sum(f.side == Side.BUY for f in fills), sell_fill_count=sum(f.side == Side.SELL for f in fills),
        rejected_order_count=outcomes['rejected'], expired_order_count=outcomes['expired'],
        win_rate_pct=100*sum(x>0 for x in pnls)/len(pnls) if pnls else None,
        average_closed_net_pnl=statistics.mean(pnls) if pnls else None,
        profit_factor=sum(x for x in pnls if x>0)/losses if losses else None,
        profit_factor_undefined_reason=None if losses else 'no_closed_losses',
        sharpe_ratio=sharpe, sharpe_undefined_reason=reason, session_return_count=len(returns),
        low_sample_count=len(returns)<30, annualization_sessions=252, risk_free_rate=0.,
        blocked_decision_count=sum(d.action.value=='blocked' for d in result.decisions),
        hold_decision_count=sum(d.action.value=='hold' for d in result.decisions))
    closed_by_exit = {t['exit_order_id']:t for t in closed}
    orders = []
    for o in result.outcomes:
        i, f = intent_map[o.order_id], o.fill
        orders.append(dict(order_id=i.order_id, symbol=i.symbol, side=i.side, decision_session=i.signal_at.session,
            eligible_execution_session=i.eligible_session, attempt_session=o.at.session,
            actual_execution_session=f.at.session if f else None, status=o.status, reason=o.reason,
            reference_open=f.reference_price if f else None, execution_price=f.price if f else None,
            filled_quantity=f.quantity if f else 0., gross_notional=f.quantity*f.price if f else None,
            fees=f.fee if f else 0., slippage_cost=f.slippage_cost if f else 0., cash_impact=o.cash_delta,
            realized_net_pnl=closed_by_exit.get(i.order_id, {}).get('realized_net_pnl')))
    symbols = sorted(data.symbols if data else {d.symbol for d in result.decisions} | set(terminal))
    per_symbol = {}
    for symbol in symbols:
        net = sum(t['realized_net_pnl'] for t in closed if t['symbol']==symbol)
        if symbol in terminal:
            p = terminal[symbol]
            net += p.quantity*p.mark.value-p.cost_basis
        per_symbol[symbol] = dict(net_pnl=net, contribution_pct=100*net/cfg.initial_cash)
    equal(sum(v['net_pnl'] for v in per_symbol.values()), summary['net_pnl'], 'Symbol contributions')
    report = dict(schema_version='primary_execution_coupled_v1', engine='primary_execution_coupled',
        config=cfg, manifest=dict(implementation_version='primary_reporting_v1', dataset_hash=result.dataset_hash,
            start=curve[0]['session'], end=curve[-1]['session'], symbols=symbols,
            valuation_mode=cfg.valuation_mode, calendar_source=cfg.calendar_policy,
            input_file_sha256=sorted(data.metadata.get('input_file_hashes',{}).values()) if data else [],
            dependencies={name:version(name) for name in ('numpy','pandas','pandas-ta','vectorbt')},
            code_hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ('primary_state.py','primary_inputs.py','primary_ledger.py','primary_marking.py','primary_strategy.py','primary_reporting.py')},
            valuation_evidence_counts=dict(eligible=sum(v.eligible for v in data.valuations),
                ineligible=sum(not v.eligible for v in data.valuations)) if data else None),
        summary=summary, closed_trades=closed, open_positions=sorted(result.terminal_positions,key=lambda p:p.symbol),
        orders=orders, fills=fills, decisions=result.decisions, per_symbol=per_symbol,
        portfolio_history=curve,
        equity_curve=[dict(session=p['session'], equity=p['equity']) for p in curve],
        cash_curve=[dict(session=p['session'], cash=p['cash']) for p in curve],
        exposure_curve=[dict(session=p['session'], exposure=p['gross_exposure']) for p in curve],
        data_quality=dict(valuation_label='historically unverified' if cfg.valuation_mode.value=='legacy_research_inputs' else 'structurally validated filing evidence; historical accuracy not certified',
            strict_point_in_time_certified=False, knowledge_contamination_status='unverified',
            observed_union_warning='May include non-exchange dates and omit sessions missing from every symbol' if cfg.calendar_policy=='observed_union' else None,
            stale_session_count=sum(bool(p['stale_symbols']) for p in curve),
            missing_sessions=data.data_quality['missing_sessions'] if data else None,
            reconciliation_tolerance=tolerance, invariant_cost='Full-journal ledger validation has known quadratic scaling'),
        windows=[])
    report = canonical_value(report)
    report['report_fingerprint'] = fingerprint(report)
    return report


def compare_primary_runs(baseline_report, coupled_result, manifest=None):
    """Report differences, never infer causality merely from higher returns."""
    primary = summarize_primary(coupled_result)
    b = baseline_report
    aliases = {'portfolio_return_pct':'total_return_pct','max_drawdown_pct':'max_drawdown_pct',
               'win_rate_pct':'win_rate_pct','ending_equity':'ending_equity'}
    differences = {}
    for key, other in aliases.items():
        x, y = primary['summary'][key], b['summary'].get(other)
        differences[key] = dict(absolute=x-y if x is not None and y is not None else None,
            relative=(x-y)/abs(y) if x is not None and y is not None and y!=0 else None)
    config_equal = all(primary['config'][k] == b['config'][k] for k in ('initial_cash','allocation_pct','fee_pct','slippage_pct'))
    mode_equal = primary['config']['valuation_mode'] == b['config']['valuation_mode']
    data_equal = primary['manifest']['dataset_hash'] == b['manifest']['dataset_hash']
    first = None
    pf, bf = primary['fills'], b['fills']
    for index in range(max(len(pf),len(bf))):
        p = pf[index] if index<len(pf) else None
        r = bf[index] if index<len(bf) else None
        if p is None or r is None or (p['symbol'],p['side'],p['at']['session'],p['quantity'],p['price']) != (r['symbol'],r['side'],r['session'],r['quantity'],r['price']):
            first = dict(index=index, primary=p, baseline=r)
            break
    report = dict(schema_version='primary_comparison_v1', primary=primary, baseline=b,
        differences=differences, equivalence=dict(configuration=config_equal, valuation_mode=mode_equal, input_data=data_equal,
            quantity_granularity=primary['config']['quantity_increment'] is None),
        first_differing_fill=first, attribution='unattributed',
        warnings=['Differences may reflect rejected-fill feedback, current-equity eligibility, sell/buy ordering, minimum notional, or quantity granularity.',
                  'Strict and legacy eligibility differences must not be attributed to execution coupling.',
                  'Baseline does not expose rejected-order journals; trade/metric conventions differ.',
                  'Synthetic comparisons are not evidence of profitability.'],
        comparison_manifest=manifest or {})
    report = canonical_value(report)
    report['report_fingerprint'] = fingerprint(report)
    return report

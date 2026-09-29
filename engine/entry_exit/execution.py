"""One serialized, journaled execution path for dashboard and both strategies.

No order is recorded as a trade until Alpaca reports a fill. Unknown submissions
remain reserved until reconciliation establishes their broker state. New buys
use whole-share limit brackets; legacy fractional holdings get DAY stops and
are reported as requiring daily renewal.
"""
from datetime import datetime, timezone
import json
import math
import uuid

from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest, GetOrdersRequest, LimitOrderRequest, MarketOrderRequest,
    StopLossRequest, StopOrderRequest, TakeProfitRequest,
)

from broker import alpaca_client as broker
from db.execution_lock import execution_lock
from db.models import BrokerBinding, ExecutionOrder, Position, Trade, SwingPosition, SwingTrade, NotificationOutbox
from db.kill_switch import is_bot_enabled, set_bot_enabled
from engine.entry_exit.risk import Limits, RiskRejected, day_start_utc, loss_breached, positive, price_tick

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


def _value(value):
    return getattr(value, "value", value)


def _bind_account(db, account):
    key = f"{'paper' if broker.PAPER else 'live'}:{account.id}"
    binding = db.get(BrokerBinding, 1)
    if binding is None:
        db.add(BrokerBinding(id=1, account_key=key))
        db.commit()
    elif binding.account_key != key:
        raise RiskRejected("Database belongs to a different broker account/mode; use a separate database")


def _positions(db, strategy):
    if strategy == "primary":
        return db.query(Position).all()
    return db.query(SwingPosition).filter(SwingPosition.status == "open").all()


def _position(db, ticker, strategy):
    return next((p for p in _positions(db, strategy) if p.ticker == ticker), None)


def _open_orders():
    orders = broker.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=False))
    if len(orders) >= 500:
        raise RiskRejected("Too many open orders to establish complete exposure")
    return orders


def _apply_fill(db, row, order):
    qty = float(order.filled_qty or 0)
    if not math.isfinite(qty) or qty < row.applied_qty:
        raise RuntimeError(f"{row.ticker}: invalid or regressing cumulative fill")
    delta = qty - row.applied_qty
    if delta <= 1e-9:
        return
    notional = qty * positive(order.filled_avg_price, "Fill price")
    price = positive((notional - row.applied_notional) / delta, "Incremental fill price")
    pos = _position(db, row.ticker, row.strategy)
    if row.side == "sell" and (pos is None or delta > pos.quantity + 1e-8):
        raise RuntimeError(f"{row.ticker}: sell fill exceeds the tracked long; manual reconciliation required")
    meta = json.loads(row.metadata_json or "{}")
    now = datetime.utcnow()
    if row.strategy == "primary":
        db.add(Trade(ticker=row.ticker, action=row.side, quantity=delta, price=price,
                     reason=row.reason, notes=f"broker_order_id={order.id}"))
        if row.side == "buy":
            if pos is None:
                pos = Position(ticker=row.ticker, quantity=0, avg_entry_price=0,
                               stop_loss_price=row.stop_price, take_profit_price=row.target_price)
                db.add(pos)
            pos.avg_entry_price = (pos.quantity * pos.avg_entry_price + delta * price) / (pos.quantity + delta)
            pos.quantity += delta
        elif pos is not None:
            pos.quantity -= delta
            if pos.quantity <= 1e-8:
                db.delete(pos)
    else:
        db.add(SwingTrade(ticker=row.ticker, action=row.side, quantity=delta, price=price,
                          reason=row.reason, order_id=str(order.id)))
        if row.side == "buy":
            if pos is None:
                pos = SwingPosition(ticker=row.ticker, quantity=0, entry_price=0,
                    stop_price=row.stop_price, target_price=row.target_price,
                    entry_timestamp=now, entry_session_date=meta.get("entry_session_date"),
                    strategy_version=meta.get("strategy_version", "unknown"),
                    config_json=meta.get("config_json"), status="open")
                if isinstance(pos.entry_session_date, str):
                    from datetime import date
                    pos.entry_session_date = date.fromisoformat(pos.entry_session_date)
                db.add(pos)
            pos.entry_price = (pos.quantity * pos.entry_price + delta * price) / (pos.quantity + delta)
            pos.quantity += delta
        elif pos is not None:
            pos.realized_pl = (pos.realized_pl or 0) + (price - pos.entry_price) * delta
            pos.quantity -= delta
            pos.exit_order_pending = False
            if pos.quantity <= 1e-8:
                pos.quantity = 0
                pos.status = "closed"
                pos.exit_price = price
                pos.exit_timestamp = now
                pos.exit_reason = row.reason
    row.applied_qty = qty
    row.applied_notional = notional
    db.add(NotificationOutbox(body=f"Trading Bot: {row.side.upper()} {delta:g} {row.ticker} "
        f"@ ${price:.2f} ({row.reason}); confirmed fill, order {order.id}"))
    db.flush()


def _record(db, row, order):
    row.broker_order_id = str(order.id)
    row.status = _value(order.status)
    _apply_fill(db, row, order)
    # Parent fills must be applied before protective child fills.
    for leg in getattr(order, "legs", None) or []:
        child = db.query(ExecutionOrder).filter(ExecutionOrder.broker_order_id == str(leg.id)).first()
        if child is None:
            child = ExecutionOrder(client_order_id=str(leg.client_order_id), broker_order_id=str(leg.id),
                parent_client_id=row.client_order_id, ticker=row.ticker, strategy=row.strategy,
                side="sell", role="protection", qty=float(leg.qty),
                reason="stop_loss" if getattr(leg, "stop_price", None) else "take_profit",
                status="submitting", applied_qty=0, applied_notional=0)
            db.add(child)
            db.flush()
        _record(db, child, leg)
    db.flush()


def _lookup(row):
    if not row.broker_order_id:
        order = broker.client.get_order_by_client_id(row.client_order_id)
        return broker.client.get_order_by_id(order.id, GetOrderByIdRequest(nested=True))
    return broker.client.get_order_by_id(row.broker_order_id, GetOrderByIdRequest(nested=True))


def _reconcile_locked(db):
    held = {p.ticker for strategy in ("primary", "swing") for p in _positions(db, strategy)}
    rows = db.query(ExecutionOrder).order_by(ExecutionOrder.created_at).all()
    errors = []
    for row in rows:
        if row.status in TERMINAL and not (row.role == "entry" and row.ticker in held):
            continue
        try:
            order = _lookup(row)
            _record(db, row, order)
            db.commit()
        except Exception as exc:
            db.rollback()
            errors.append(f"{row.ticker}: reconciliation required ({type(exc).__name__})")
    return errors


def reconcile_orders(db):
    with execution_lock(db):
        _bind_account(db, broker.get_account())
        return _reconcile_locked(db)


def _submit(db, request, *, strategy, role, reason, stop=None, target=None, metadata=None):
    row = ExecutionOrder(client_order_id=request.client_order_id, ticker=request.symbol,
        strategy=strategy, side=_value(request.side), role=role, qty=float(request.qty),
        limit_price=getattr(request, "limit_price", None), stop_price=stop,
        target_price=target, reason=reason, metadata_json=json.dumps(metadata or {}),
        status="submitting", applied_qty=0, applied_notional=0)
    db.add(row)
    db.commit()  # Durable reservation MUST precede the network call.
    try:
        order = broker._submit_order(request)
    except Exception:
        # Includes timeouts AND HTTP errors: never infer rejection from a transport
        # failure. Resolve the persisted client ID on a later reconciliation pass.
        row.status = "unknown"
        db.commit()
        raise RuntimeError(f"{row.ticker}: submission outcome unknown; reserved as {row.client_order_id}. Do not retry.") from None
    _record(db, row, order)
    db.commit()
    if row.status == "rejected":
        raise RiskRejected(f"{row.ticker}: broker rejected the {role} order {order.id}")
    return {"ticker": row.ticker, "action": row.side, "qty": row.applied_qty,
            "price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "order_id": str(order.id), "status": row.status, "requested_qty": row.qty}


def _client_id():
    return "tb-" + uuid.uuid4().hex


def execute_buy(db, ticker, qty=None, reason="manual", stop_loss_price=None,
                take_profit_price=None, *, strategy="primary", metadata=None, qty_cap=None):
    ticker = ticker.upper()
    if strategy not in {"primary", "swing"}:
        raise RiskRejected("Unknown strategy")
    with execution_lock(db):
        account = broker.get_account()
        _bind_account(db, account)
        broker.assert_trading_mode(account)
        if strategy == "swing" and not broker.PAPER:
            raise RiskRejected("Experimental swing execution is paper-only")
        if _reconcile_locked(db):
            raise RiskRejected("Reconciliation must succeed before new entries")
        if maintain_protection(db):
            raise RiskRejected("Existing positions need attention before new entries")
        if not is_bot_enabled():
            raise RiskRejected("New entries are paused; protective exits remain enabled")
        if reason != "manual":
            from db.autonomous_mode import is_autonomous_enabled
            from db.swing_autonomous_mode import is_swing_autonomous_enabled
            enabled = is_autonomous_enabled() if strategy == "primary" else is_swing_autonomous_enabled()
            if not enabled:
                raise RiskRejected("Autonomous entries are disabled")
            from notifications.monitoring import health_state
            if not health_state(db)["healthy"]:
                raise RiskRejected("Unattended entries require healthy monitoring, SMS and external heartbeat configuration")
        # Re-read after reconciliation/protection network calls.
        account = broker.get_account()
        limits = Limits.from_env()
        if loss_breached(account, limits):
            set_bot_enabled(False, reason="daily_loss_limit_hit")
            raise RiskRejected("Daily loss limit reached; entries paused")
        if account.trading_blocked or getattr(account, "account_blocked", False):
            raise RiskRejected("Broker account is blocked")
        if not broker.client.get_clock().is_open:
            raise RiskRejected("Entries are allowed only during regular market hours")
        if db.query(ExecutionOrder).filter(ExecutionOrder.status.in_(["unknown", "submitting"])).first():
            raise RiskRejected("An unresolved submission is reserving entry capacity")
        positions = broker.get_positions()
        if any(p.symbol == ticker for p in positions) or any(_position(db, ticker, s) for s in ("primary", "swing")):
            raise RiskRejected("Already holding this ticker; averaging in is disabled")
        orders = _open_orders()
        if any(o.symbol == ticker for o in orders):
            raise RiskRejected("Ticker already has an open order")
        start = day_start_utc()
        local_count = db.query(ExecutionOrder).filter(ExecutionOrder.role == "entry", ExecutionOrder.created_at >= start).count()
        today = broker.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=start.replace(tzinfo=timezone.utc), limit=500, nested=False))
        if len(today) >= 500:
            raise RiskRejected("Cannot establish complete daily order count")
        external_count = sum(_value(o.side) == "buy" and not db.get(ExecutionOrder, str(o.client_order_id)) for o in today)
        if local_count + external_count >= limits.max_entries:
            raise RiskRejected("Daily entry limit reached")
        equity = positive(account.equity, "Account equity")
        ask = broker.fresh_ask(ticker)
        price = price_tick(ask * 1.001)
        budget = min(limits.max_trade, equity * limits.max_position_pct)
        if qty is None:
            qty = math.floor(budget / price)
            if qty_cap is not None:
                qty = min(qty, math.floor(positive(qty_cap, "Strategy quantity cap")))
        qty = positive(qty, "Quantity")
        if not qty.is_integer():
            raise RiskRejected("New entries require whole shares for persistent broker bracket protection")
        cost = qty * price
        if cost < 1:
            raise RiskRejected("Entry is below the broker's $1 minimum")
        if cost > budget + 1e-8:
            raise RiskRejected("Entry exceeds per-trade or per-position cap")
        reserved = 0.0
        for order in orders:
            if _value(order.side) == "buy":
                # An external market buy has no bounded execution price.
                if not getattr(order, "limit_price", None):
                    raise RiskRejected("Unbounded pending buy prevents safe exposure calculation")
                reserved += max(0, float(order.qty) - float(order.filled_qty or 0)) * positive(order.limit_price, "Pending order price")
        exposure = sum(abs(float(p.market_value)) for p in positions)
        if not math.isfinite(exposure) or exposure + reserved + cost > equity * limits.max_exposure_pct + 1e-8:
            raise RiskRejected("Total account exposure cap reached")
        cash = float(account.cash)
        buying_power = float(account.buying_power)
        if not all(math.isfinite(v) for v in (cash, buying_power)) or cost + reserved > min(cash, buying_power) + 1e-8:
            raise RiskRejected("Insufficient unreserved cash; margin borrowing is disabled")
        stop = price_tick(positive(stop_loss_price, "Stop price"))
        target = price_tick(positive(take_profit_price, "Target price"))
        if not 0 < stop <= min(ask, price) - 0.01 or target <= price:
            raise RiskRejected("Require a stop below the current entry price and a target above it")
        request = LimitOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
            limit_price=price, time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=stop), take_profit=TakeProfitRequest(limit_price=target),
            client_order_id=_client_id())
        return _submit(db, request, strategy=strategy, role="entry", reason=reason,
                       stop=stop, target=target, metadata=metadata)


def _cancel(db, row):
    order = _lookup(row)
    _record(db, row, order)
    db.commit()
    if row.status not in TERMINAL:
        broker.client.cancel_order_by_id(order.id)
        _record(db, row, _lookup(row))
        db.commit()
    if row.status not in TERMINAL:
        raise RiskRejected(f"{row.ticker}: cancellation pending; wait for reconciliation")


def execute_sell(db, ticker, qty, reason, *, strategy="primary"):
    ticker = ticker.upper()
    qty = positive(qty, "Quantity")
    with execution_lock(db):
        _bind_account(db, broker.get_account())
        broker.assert_trading_mode()
        if strategy == "swing" and not broker.PAPER:
            raise RiskRejected("Experimental swing execution is paper-only")
        other_strategy = "swing" if strategy == "primary" else "primary"
        if _position(db, ticker, other_strategy):
            raise RiskRejected("Ticker belongs to another strategy; resolve ownership before selling")
        _reconcile_locked(db)
        if not broker.client.get_clock().is_open:
            raise RiskRejected("Market exits require an open regular session; broker stops remain in place")
        pending = db.query(ExecutionOrder).filter(ExecutionOrder.ticker == ticker, ~ExecutionOrder.status.in_(TERMINAL)).all()
        for row in pending:
            if row.strategy != strategy or row.role == "exit":
                raise RiskRejected("An exit or another strategy's order is already pending")
            _cancel(db, row)
        # A stop may have filled during cancellation. Refresh BOTH sources before
        # selling, never blindly submit the original dashboard quantity.
        if any(error.startswith(ticker + ":") for error in _reconcile_locked(db)):
            raise RiskRejected("Resolve order state before submitting an exit")
        held = next((p for p in broker.get_positions() if p.symbol == ticker), None)
        pos = _position(db, ticker, strategy)
        if held is None:
            return {"ticker": ticker, "action": "sell", "qty": 0, "status": "already_closed"}
        if pos is None:
            raise RiskRejected("Position is not tracked by this strategy")
        if float(held.qty) <= 0:
            raise RiskRejected("Only reducing long positions is supported")
        if any(o.symbol == ticker for o in _open_orders()):
            raise RiskRejected("Broker still has open orders for this ticker; exit deferred")
        qty = min(qty, float(held.qty), pos.quantity)
        return _submit(db, MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=_client_id()),
            strategy=strategy, role="exit", reason=reason)


def maintain_protection(db):
    """Run even when entry switches are OFF. Returns actionable health issues."""
    with execution_lock(db):
        account = broker.get_account()
        _bind_account(db, account)
        issues = _reconcile_locked(db)
        clock = broker.client.get_clock()
        # Cancel stale/partially-filled entries; bracket legs only activate after
        # full fill. Cancellation is confirmed before attaching a standalone stop.
        for row in db.query(ExecutionOrder).filter(ExecutionOrder.role == "entry", ~ExecutionOrder.status.in_(TERMINAL)).all():
            age = (datetime.utcnow() - row.created_at).total_seconds()
            from db.autonomous_mode import is_autonomous_enabled
            from db.swing_autonomous_mode import is_swing_autonomous_enabled
            entry_enabled = (row.reason == "manual" or
                (is_autonomous_enabled() if row.strategy == "primary" else is_swing_autonomous_enabled()))
            if age >= 60 or row.applied_qty or not clock.is_open or not is_bot_enabled() or not entry_enabled:
                try:
                    _cancel(db, row)
                except Exception as exc:
                    db.rollback()
                    issues.append(f"{row.ticker}: entry cancellation unresolved ({type(exc).__name__})")
        holdings = {p.symbol: p for p in broker.get_positions()}
        for strategy in ("primary", "swing"):
            for pos in _positions(db, strategy):
                try:
                    if _position(db, pos.ticker, "swing" if strategy == "primary" else "primary"):
                        issues.append(f"{pos.ticker}: ambiguous strategy ownership; reconcile manually")
                        continue
                    held = holdings.get(pos.ticker)
                    if held is None or abs(float(held.qty) - pos.quantity) > 1e-6:
                        issues.append(f"{pos.ticker}: broker/database quantity mismatch; reconcile manually")
                        continue
                    qty = positive(held.qty, "Held quantity")
                    stop = pos.stop_loss_price if strategy == "primary" else pos.stop_price
                    stop = price_tick(positive(stop, "Existing position stop"))
                    if not qty.is_integer():
                        issues.append(f"{pos.ticker}: fractional holding has DAY-only protection; renewal requires running service")
                    pending = db.query(ExecutionOrder).filter(ExecutionOrder.ticker == pos.ticker, ~ExecutionOrder.status.in_(TERMINAL)).all()
                    if any(r.role in {"entry", "exit"} or r.status in {"unknown", "submitting"} for r in pending):
                        issues.append(f"{pos.ticker}: order pending; protection cannot yet be confirmed")
                        continue
                    orders = [o for o in _open_orders() if o.symbol == pos.ticker]
                    stops = [o for o in orders if _value(o.side) == "sell" and getattr(o, "stop_price", None)
                             and float(o.stop_price) >= stop
                             and _value(getattr(o, "type", None)) == "stop"
                             and _value(o.time_in_force) == ("gtc" if qty.is_integer() else "day")
                             and _value(o.status) in {"new", "accepted", "partially_filled"}]
                    if any(float(o.qty) - float(o.filled_qty or 0) >= qty - 1e-8 for o in stops):
                        continue
                    if orders:
                        issues.append(f"{pos.ticker}: open orders exist but full stop coverage is unconfirmed")
                        continue
                    if not clock.is_open:
                        issues.append(f"{pos.ticker}: missing broker stop; will attach during regular session")
                        continue
                    if float(held.current_price) <= stop:
                        execute_sell(db, pos.ticker, qty, "stop_loss", strategy=strategy)
                        continue
                    broker.assert_trading_mode(account)
                    if strategy == "swing" and not broker.PAPER:
                        raise RiskRejected("Swing is paper-only")
                    result = _submit(db, StopOrderRequest(symbol=pos.ticker, qty=qty, side=OrderSide.SELL,
                        stop_price=stop, time_in_force=TimeInForce.GTC if qty.is_integer() else TimeInForce.DAY,
                        client_order_id=_client_id()), strategy=strategy, role="protection", reason="stop_loss", stop=stop)
                    if result["status"] not in {"new", "accepted", "partially_filled", "filled"}:
                        issues.append(f"{pos.ticker}: broker has not yet confirmed protection acceptance")
                except Exception as exc:
                    db.rollback()
                    issues.append(f"{pos.ticker}: protection failed ({type(exc).__name__}: {exc})")
        return issues

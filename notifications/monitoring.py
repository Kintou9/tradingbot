"""Durable worker health, throttled alerts, external heartbeat and daily digest."""
from datetime import datetime, timezone
import json
import os
from zoneinfo import ZoneInfo

import requests
from alpaca.trading.requests import GetCalendarRequest

from broker import alpaca_client as broker
from db.models import ExecutionOrder, RuntimeState, NotificationOutbox
from db.session import SessionLocal
from db.execution_lock import execution_lock, ExecutionBusy
from engine.entry_exit.execution import TERMINAL
from notifications.sms import send_sms, is_configured


def save_state(db, key, value):
    row = db.get(RuntimeState, key)
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps(value)
    row.updated_at = datetime.utcnow()
    db.commit()


def health_state(db):
    row = db.get(RuntimeState, "supervisor")
    if row is None:
        return {"healthy": False, "issues": ["Waiting for first completed protection cycle"]}
    state = json.loads(row.value)
    age = (datetime.utcnow() - row.updated_at).total_seconds()
    issues = list(state.get("issues", []))
    if age > 120:
        issues.append("Protection worker heartbeat is stale")
    if not is_configured():
        issues.append("SMS notifications are not configured")
    if not os.getenv("HEARTBEAT_URL"):
        issues.append("External dead-man heartbeat monitor is not configured")
    return {"healthy": not issues, "issues": issues, "last_cycle_at": row.updated_at.isoformat() + "Z",
            "age_seconds": round(age), "sms_configured": is_configured(),
            "heartbeat_configured": bool(os.getenv("HEARTBEAT_URL"))}


def report_cycle(db, issues):
    issues = sorted(set(issues))
    pending = db.query(NotificationOutbox).filter(NotificationOutbox.sent_at.is_(None)).order_by(NotificationOutbox.id).limit(10).all()
    for message in pending:
        if not send_sms(message.body):
            issues.append("Queued notification delivery failed")
            break
        message.sent_at = datetime.utcnow()
        db.commit()
    # Only signal success when the protection cycle actually completed cleanly.
    # A remote monitor must alert on absent pings; a dead process cannot alert itself.
    url = os.getenv("HEARTBEAT_URL")
    if url and not issues:
        try:
            if not url.startswith("https://"):
                raise ValueError("Heartbeat URL must use HTTPS")
            response = requests.get(url, timeout=5, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Heartbeat rejected")
        except Exception:
            issues.append("External heartbeat delivery failed")
    previous = db.get(RuntimeState, "last_safety_alert")
    if issues and (previous is None or (datetime.utcnow() - previous.updated_at).total_seconds() >= 900):
        if send_sms("Trading Bot needs attention: " + "; ".join(issues)[:1000]):
            save_state(db, "last_safety_alert", issues)
        else:
            issues.append("Safety alert could not be delivered")
    save_state(db, "supervisor", {"issues": issues})


def send_account_digest(db):
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    calendar = broker.client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    if not calendar or now.time().replace(tzinfo=None) < calendar[0].close.time():
        return
    key = "account_digest:" + now.date().isoformat()
    if db.get(RuntimeState, key):
        return
    account = broker.get_account()
    positions = broker.get_positions()
    pending = db.query(ExecutionOrder).filter(~ExecutionOrder.status.in_(TERMINAL)).count()
    health = health_state(db)
    body = (f"Trading Bot {'PAPER' if broker.PAPER else 'LIVE'} daily summary: "
            f"equity ${float(account.equity):.2f}, daily change "
            f"${float(account.equity) - float(account.last_equity):+.2f}; "
            f"{len(positions)} holdings, {pending} open/uncertain tracked orders; "
            f"{len(health['issues'])} health issues. "
            + ", ".join(f"{p.symbol} {p.qty}" for p in positions))
    if send_sms(body):
        save_state(db, key, {"sent": True})


def supervisor_tick(run_cycle):
    db = SessionLocal()
    try:
        with execution_lock(db):
            try:
                result = run_cycle()
                issues = result.get("issues", [])
            except Exception as exc:
                issues = [f"Protection cycle failed: {type(exc).__name__}: {exc}"]
            report_cycle(db, issues)
            try:
                send_account_digest(db)
            except Exception as exc:
                report_cycle(db, issues + [f"Account digest failed: {type(exc).__name__}"])
    except ExecutionBusy:
        # Another process is doing the work. It must write its own completion
        # heartbeat; a skipped tick must never mask a stalled worker.
        pass
    finally:
        db.close()

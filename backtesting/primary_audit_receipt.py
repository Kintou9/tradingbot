"""Deterministic content receipts for independently reconciled simulations.

SHA-256 here records integrity and reproducibility of supplied evidence. It is
unkeyed: it does not authenticate the evidence's author, external truth, filing
availability, window-policy origin, profitability, or trading correctness.
"""
import hashlib
import json
import math

from backtesting.primary_inputs import canonical_json
from backtesting.primary_state import JournalAuditReceipt


SCHEMA_VERSION = "primary_journal_audit_v1"
ALGORITHM = "sha256"
EVIDENCE_FIELDS = frozenset(("input", "config", "journal", "snapshots", "terminal"))


class AuditReceiptError(ValueError):
    pass


def canonical_evidence_bytes(value):
    """Canonical UTF-8 JSON; binary64 values retain Python's exact JSON spelling.

    `-0.0` remains byte-distinct from `0.0`. NaN, infinities, unsupported values,
    naive datetimes and non-string mapping keys are rejected by canonical_json.
    """
    try:
        return canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditReceiptError("Evidence is not canonically serializable") from exc


def _digest(value):
    return hashlib.sha256(canonical_evidence_bytes(value)).hexdigest()


def build_audit_evidence(*, dataset_hash, manifest_json, config, submitted, outcomes,
                         events, snapshots, terminal_state, terminal_reason,
                         lifecycle_stage):
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as exc:
        raise AuditReceiptError("Invalid prepared-input provenance JSON") from exc
    if not isinstance(manifest, dict):
        raise AuditReceiptError("Prepared-input provenance must be an object")
    ordered_submitted = [submitted[key] for key in sorted(submitted)]
    ordered_positions = [terminal_state.positions[key] for key in sorted(terminal_state.positions)]
    outcomes = tuple(outcomes)
    return {
        "input": {"dataset_hash": dataset_hash, "prepared_provenance": manifest},
        "config": config,
        "journal": {
            "submitted": ordered_submitted,
            "outcomes": outcomes,
            "fills": [outcome.fill for outcome in outcomes if outcome.fill is not None],
            "events": tuple(events),
        },
        "snapshots": tuple(snapshots),
        "terminal": {
            "positions": ordered_positions,
            "cash": terminal_state.cash,
            "equity": terminal_state.equity,
            "realized_net_pnl": terminal_state.realized_net_pnl,
            "realized_gross_pnl": terminal_state.realized_gross_pnl,
            "fees_paid": terminal_state.fees_paid,
            "slippage_costs": terminal_state.slippage_costs,
            "pending": [terminal_state.pending[key] for key in sorted(terminal_state.pending)],
            "terminal_reason": terminal_reason,
            "lifecycle_stage": lifecycle_stage,
            "event_time": terminal_state.at,
        },
    }


def _validate_evidence_shape(evidence):
    if type(evidence) is not dict or set(evidence) != EVIDENCE_FIELDS:
        raise AuditReceiptError("Audit evidence has missing or unexpected fields")
    if type(evidence["input"]) is not dict or set(evidence["input"]) != {"dataset_hash", "prepared_provenance"}:
        raise AuditReceiptError("Invalid input evidence fields")
    if type(evidence["journal"]) is not dict or set(evidence["journal"]) != {"submitted", "outcomes", "fills", "events"}:
        raise AuditReceiptError("Invalid journal evidence fields")
    required_terminal = {"positions", "cash", "equity", "realized_net_pnl", "realized_gross_pnl",
                         "fees_paid", "slippage_costs", "pending", "terminal_reason",
                         "lifecycle_stage", "event_time"}
    if type(evidence["terminal"]) is not dict or set(evidence["terminal"]) != required_terminal:
        raise AuditReceiptError("Invalid terminal evidence fields")


def _expected(evidence):
    _validate_evidence_shape(evidence)
    journal = evidence["journal"]
    counts = dict(submitted_count=len(journal["submitted"]), outcome_count=len(journal["outcomes"]),
                  fill_count=len(journal["fills"]), event_count=len(journal["events"]),
                  snapshot_count=len(evidence["snapshots"]))
    terminal_reason = evidence["terminal"]["terminal_reason"]
    return dict(schema_version=SCHEMA_VERSION, algorithm=ALGORITHM,
        evidence_digest=_digest(evidence), input_digest=_digest(evidence["input"]),
        config_digest=_digest(evidence["config"]), journal_digest=_digest(journal),
        snapshot_digest=_digest(evidence["snapshots"]),
        terminal_state_digest=_digest(evidence["terminal"]),
        terminal_reason=terminal_reason, **counts)


def create_audit_receipt(evidence):
    return JournalAuditReceipt(**_expected(evidence))


def verify_audit_receipt(receipt, evidence):
    if not isinstance(receipt, JournalAuditReceipt):
        raise AuditReceiptError("Unsupported receipt object")
    try:
        expected = JournalAuditReceipt(**_expected(evidence))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, AuditReceiptError):
            raise
        raise AuditReceiptError("Malformed audit evidence") from exc
    if receipt != expected:
        raise AuditReceiptError("Journal audit receipt does not match supplied evidence")
    return True

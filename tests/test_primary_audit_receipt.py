"""Deterministic receipt creation and verification for reconciled evidence."""
import copy
from dataclasses import replace

import pytest

from backtesting.primary_audit_receipt import (
    AuditReceiptError, build_audit_evidence, canonical_evidence_bytes,
    create_audit_receipt, verify_audit_receipt,
)
from backtesting.primary_state import ExpirationReason, OrderStatus, PrimarySimulationConfig
from backtesting.primary_strategy import run_primary_strategy
from backtesting.primary_windows import prepare_window, window_spec
from tests.test_primary_ledger import dataset
from tests.test_primary_reconciliation import completed


def evidence(ledger):
    return build_audit_evidence(dataset_hash=ledger.data.content_hash,
        manifest_json=ledger.data.metadata_json, config=ledger.config,
        submitted=ledger._submitted, outcomes=ledger.outcomes, events=ledger.events,
        snapshots=ledger._history.view(), terminal_state=ledger._state,
        terminal_reason=ledger._generation.terminal_reason,
        lifecycle_stage=ledger._generation.stage)


def test_repeated_runs_and_result_access_have_identical_receipts():
    left, first = completed()
    right, second = completed()
    assert first.audit_receipt == left.result().audit_receipt
    assert first.audit_receipt == second.audit_receipt
    assert verify_audit_receipt(first.audit_receipt, evidence(left)) is True
    before = left._generation
    assert verify_audit_receipt(first.audit_receipt, evidence(left)) is True
    assert left._generation is before


def test_receipt_section_digests_bind_their_claimed_evidence():
    ledger, result = completed(open_position=True)
    original = evidence(ledger)
    receipt = result.audit_receipt
    mutations = []
    changed = copy.deepcopy(original); changed['input']['dataset_hash'] = 'f'*64; mutations.append(('input_digest', changed))
    changed = copy.deepcopy(original); changed['input']['prepared_provenance']['price_source'] = 'changed'; mutations.append(('input_digest', changed))
    changed = copy.deepcopy(original); changed['config'] = replace(ledger.config, initial_cash=1001.); mutations.append(('config_digest', changed))
    changed = copy.deepcopy(original); changed['journal']['submitted'][0] = replace(changed['journal']['submitted'][0], reason='changed'); mutations.append(('journal_digest', changed))
    changed = copy.deepcopy(original); changed['journal']['events'] = tuple(reversed(changed['journal']['events'])); mutations.append(('journal_digest', changed))
    changed = copy.deepcopy(original); changed['snapshots'] = list(changed['snapshots']); changed['snapshots'][0] = replace(changed['snapshots'][0], cash=999., equity=999.); mutations.append(('snapshot_digest', changed))
    changed = copy.deepcopy(original); changed['terminal']['cash'] += 1; mutations.append(('terminal_state_digest', changed))
    changed = copy.deepcopy(original); changed['terminal']['equity'] += 1; mutations.append(('terminal_state_digest', changed))
    changed = copy.deepcopy(original); changed['terminal']['realized_net_pnl'] += 1; mutations.append(('terminal_state_digest', changed))
    changed = copy.deepcopy(original); changed['terminal']['terminal_reason'] = ExpirationReason.END_OF_WINDOW; mutations.append(('terminal_state_digest', changed))
    changed = copy.deepcopy(original); position = copy.copy(changed['terminal']['positions'][0]); object.__setattr__(position, 'quantity', position.quantity+1); changed['terminal']['positions'][0] = position; mutations.append(('terminal_state_digest', changed))
    changed = copy.deepcopy(original); position = copy.copy(changed['terminal']['positions'][0]); object.__setattr__(position, 'cost_basis', position.cost_basis+1); changed['terminal']['positions'][0] = position; mutations.append(('terminal_state_digest', changed))
    for section, changed in mutations:
        replacement = create_audit_receipt(changed)
        assert getattr(replacement, section) != getattr(receipt, section)
        assert replacement.evidence_digest != receipt.evidence_digest
        with pytest.raises(AuditReceiptError, match='does not match'):
            verify_audit_receipt(receipt, changed)

    ordered_ledger, ordered_result = completed()
    changed = evidence(ordered_ledger)
    changed['journal']['outcomes'] = tuple(reversed(changed['journal']['outcomes']))
    assert create_audit_receipt(changed).journal_digest != ordered_result.audit_receipt.journal_digest
    changed = evidence(ordered_ledger)
    outcome = copy.copy(changed['journal']['outcomes'][0])
    object.__setattr__(outcome, 'status', OrderStatus.REJECTED)
    outcomes = list(changed['journal']['outcomes']); outcomes[0] = outcome
    changed['journal']['outcomes'] = outcomes
    assert create_audit_receipt(changed).journal_digest != ordered_result.audit_receipt.journal_digest


@pytest.mark.parametrize('field', ['reference_price', 'price', 'fee', 'slippage_cost', 'cash_delta'])
def test_fill_economics_change_journal_digest(field):
    ledger, result = completed()
    changed = evidence(ledger)
    outcome = copy.copy(changed['journal']['outcomes'][0])
    fill = copy.copy(outcome.fill)
    object.__setattr__(fill, field, getattr(fill, field)+1)
    object.__setattr__(outcome, 'fill', fill)
    outcomes = list(changed['journal']['outcomes']); outcomes[0] = outcome
    changed['journal']['outcomes'] = outcomes
    changed['journal']['fills'][0] = fill
    replacement = create_audit_receipt(changed)
    assert replacement.journal_digest != result.audit_receipt.journal_digest


def test_snapshot_mark_cash_and_equity_are_bound():
    ledger, result = completed(open_position=True)
    base = evidence(ledger)
    for kind in ('mark', 'cash', 'equity'):
        changed = copy.deepcopy(base)
        snapshots = list(changed['snapshots'])
        index = next(i for i, snapshot in enumerate(snapshots) if snapshot.marks)
        snapshot = snapshots[index]
        if kind == 'mark':
            symbol, mark = snapshot.marks[0]
            snapshots[index] = replace(snapshot, marks=((symbol, replace(mark, value=mark.value+1)),))
        elif kind == 'cash':
            snapshots[index] = replace(snapshot, cash=snapshot.cash+1, equity=snapshot.equity+1)
        else:
            object.__setattr__(snapshot, 'equity', snapshot.equity+1)
        changed['snapshots'] = snapshots
        assert create_audit_receipt(changed).snapshot_digest != result.audit_receipt.snapshot_digest


def test_negative_zero_is_distinct_and_nonfinite_or_unsupported_values_fail():
    assert canonical_evidence_bytes(-0.0) != canonical_evidence_bytes(0.0)
    for value in (float('nan'), float('inf'), -float('inf'), {1: 'bad'}, object()):
        with pytest.raises(AuditReceiptError):
            canonical_evidence_bytes(value)


def test_verification_rejects_schema_algorithm_counts_digests_and_evidence_shape():
    ledger, result = completed()
    receipt = result.audit_receipt
    original = evidence(ledger)
    for field, value in [('schema_version', 'unknown'), ('algorithm', 'sha512'),
                         ('outcome_count', receipt.outcome_count+1),
                         ('evidence_digest', '0'*64)]:
        altered = copy.copy(receipt)
        object.__setattr__(altered, field, value)
        with pytest.raises(AuditReceiptError):
            verify_audit_receipt(altered, original)
    for changed in ({}, dict(original, unexpected=True), {k:v for k,v in original.items() if k!='journal'}):
        with pytest.raises(AuditReceiptError):
            verify_audit_receipt(receipt, changed)


def test_independent_window_boundaries_have_deterministic_distinct_receipts():
    data = dataset(6, symbols=('A',))
    config = PrimarySimulationConfig(1000., .5, valuation_mode=data.valuation_mode,
                                     calendar_policy=data.metadata['calendar_source'])
    one = prepare_window(data, window_spec('one', data.sessions[0], data.sessions[2]))
    two = prepare_window(data, window_spec('two', data.sessions[1], data.sessions[3]))
    first = run_primary_strategy(one, config).audit_receipt
    repeated = run_primary_strategy(one, config).audit_receipt
    second = run_primary_strategy(two, config).audit_receipt
    assert first == repeated and first.input_digest != second.input_digest


def test_initial_cash_changes_config_and_whole_evidence_digest():
    data = dataset(3, symbols=('A',))
    first = run_primary_strategy(data, PrimarySimulationConfig(1000., .5)).audit_receipt
    second = run_primary_strategy(data, PrimarySimulationConfig(2000., .5)).audit_receipt
    assert first.config_digest != second.config_digest
    assert first.evidence_digest != second.evidence_digest


def test_receipt_serialization_failure_does_not_mutate_or_cache_result(monkeypatch):
    import backtesting.primary_audit_receipt as module
    ledger, expected = completed()
    generation = ledger._generation
    with monkeypatch.context() as patch:
        patch.setattr(module, 'canonical_evidence_bytes',
                      lambda value: (_ for _ in ()).throw(AuditReceiptError('injected serialization')))
        with pytest.raises(AuditReceiptError, match='injected serialization'):
            ledger.result()
    assert ledger._generation is generation
    assert ledger.result() == expected

"""Synthetic offline preparation and provenance tests."""
from dataclasses import replace
from datetime import date, datetime, timezone
import socket

import numpy as np
import pandas as pd
import pytest

from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_state import ValuationObservation, FactEvidence, ProvenanceStatus

META = dict(calendar_source='observed_union', price_source='synthetic', adjustment_policy='none')
HASH = 'a'*64


def bars(n=260):
    x = np.arange(n)
    close = 100 + x*.03 + np.sin(x/3)
    return pd.DataFrame(dict(open=close-.1, high=close+1, low=close-1, close=close,
                             volume=np.full(n, 1000.)), index=pd.bdate_range('2023-01-02', periods=n))


def valuation(**kw):
    args = dict(symbol='A', filed_date=date(2023, 1, 1), effective_available_date=date(2023, 1, 2),
        fair_value=200., share_count_source='filing', filing_context_hash=HASH,
        raw_output_hash='b'*64, model_version='captured-test-model', prompt_version='test-v1',
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), required_fact_ids=('shares', 'revenue'),
        fact_evidence=(FactEvidence('shares', date(2022, 12, 31), 'filing-1', HASH),
                       FactEvidence('revenue', date(2022, 12, 31), 'filing-1', HASH)))
    return ValuationObservation(**(args | kw))


def prepare(frame=None, records=(), metadata=None):
    frame = bars() if frame is None else frame
    return prepare_primary_data({'A': frame}, records, session_index=frame.index, metadata=metadata or META)


def test_valid_inputs_preserve_values_and_defend_against_mutation():
    frame = bars()
    original = frame.copy(deep=True)
    result = prepare(frame, [valuation()])
    pd.testing.assert_frame_equal(frame, original)
    pd.testing.assert_frame_equal(result.prices_by_symbol['A'][list(frame.columns)], frame, check_freq=False)
    frame.iloc[0, 0] = 999
    returned = result.prices_by_symbol
    returned['A'].iloc[0, 0] = 888
    assert result.prices_by_symbol['A'].iloc[0]['open'] == original.iloc[0]['open']
    meta = result.metadata
    meta['price_source'] = 'changed'
    assert result.metadata['price_source'] == 'synthetic'


def test_symbol_order_and_hash_are_canonical():
    frame = bars()
    a = prepare_primary_data({' b ': frame, 'a': frame}, [valuation(symbol='B'), valuation()], session_index=frame.index, metadata=META)
    b = prepare_primary_data({'A': frame, 'B': frame}, [valuation(), valuation(symbol='B')], session_index=frame.index, metadata=dict(reversed(list(META.items()))))
    assert a.symbols == ('A', 'B')
    assert a.content_hash == b.content_hash
    assert a.frames == b.frames and a.valuations == b.valuations


def test_symbol_collision():
    frame = bars()
    with pytest.raises(ValueError, match='collision'):
        prepare_primary_data({'a': frame, ' A ': frame}, [], session_index=frame.index, metadata=META)


@pytest.mark.parametrize('column,value', [('high', 1), ('low', 200), ('open', 0),
    ('close', float('nan')), ('high', float('inf')), ('volume', -1), ('volume', float('nan'))])
def test_invalid_ohlcv(column, value):
    frame = bars()
    frame.loc[frame.index[0], column] = value
    with pytest.raises(ValueError):
        prepare(frame)


def test_missing_bars_are_recorded_not_filled():
    frame = bars()
    sparse = frame.drop(frame.index[30])
    result = prepare_primary_data({'A': sparse}, [], session_index=frame.index, metadata=META)
    assert result.data_quality['missing_sessions']['A'] == [frame.index[30].date().isoformat()]
    assert len(result.prices_by_symbol['A']) == len(sparse)
    with pytest.raises(ValueError, match='outside'):
        prepare_primary_data({'A': frame}, [], session_index=sparse.index, metadata=META)


@pytest.mark.parametrize('kind', ['duplicate', 'unsorted', 'intraday', 'ambiguous', 'empty'])
def test_invalid_dates(kind):
    frame = bars()
    if kind == 'duplicate':
        frame.index = pd.DatetimeIndex([frame.index[0]]*2 + list(frame.index[2:]))
    elif kind == 'unsorted':
        frame = frame.iloc[::-1]
    elif kind == 'intraday':
        frame.index = frame.index + pd.Timedelta(hours=12)
    elif kind == 'ambiguous':
        frame.index = ['01/02/2023']*len(frame)
    else:
        frame = frame.iloc[:0]
    with pytest.raises(ValueError):
        prepare(frame)


def test_timezone_normalization_preserves_exchange_date():
    frame = bars()
    aware = frame.copy()
    aware.index = aware.index.tz_localize('America/New_York').tz_convert('UTC')
    assert prepare(frame).content_hash == prepare(aware).content_hash
    # Midnight UTC would map to the prior evening: don't silently shift a daily label.
    aware.index = frame.index.tz_localize('UTC')
    with pytest.raises(ValueError, match='midnight'):
        prepare(aware)


def test_primary_indicator_prefix_invariance():
    frame = bars()
    full = prepare(frame).prices_by_symbol['A']
    prefix = prepare(frame.iloc[:220]).prices_by_symbol['A']
    columns = ['close', 'sma_50', 'support', 'resistance']
    pd.testing.assert_frame_equal(full.loc[prefix.index, columns], prefix[columns])
    changed = frame.copy()
    changed.iloc[220:, :4] *= 2
    other = prepare(changed).prices_by_symbol['A']
    pd.testing.assert_frame_equal(full.iloc[:220][columns], other.iloc[:220][columns])


def test_short_history_has_no_fabricated_warmup():
    for n in (1, 20, 34, 35, 49, 50):
        result = prepare(bars(n)).prices_by_symbol['A']
        assert len(result) == n
        assert result.sma_50.iloc[:49].isna().all()
        if n == 50:
            assert np.isfinite(result.sma_50.iloc[-1])


@pytest.mark.parametrize('change', ['price', 'metadata', 'valuation', 'mode'])
def test_hash_changes_for_meaningful_input(change):
    frame, records, meta = bars(), [valuation()], dict(META)
    before = prepare(frame, records, meta).content_hash
    if change == 'price':
        frame.iloc[0, frame.columns.get_loc('volume')] += 1
    elif change == 'metadata':
        meta['adjustment_policy'] = 'split_adjusted'
    elif change == 'valuation':
        records = [valuation(fair_value=201.)]
    else:
        meta['valuation_mode'] = 'legacy_research_inputs'
    assert prepare(frame, records, meta).content_hash != before


def test_verified_valuation_evidence_and_boundary():
    result = prepare(records=[valuation()])
    assert result.valuation_asof('A', date(2023, 1, 1)) is None
    item = result.valuation_asof('A', date(2023, 1, 2))
    assert item.eligible and item.provenance == ProvenanceStatus.VERIFIED
    assert item.observation.generated_at.year == 2026
    assert not result.data_quality['strict_point_in_time_certified']
    assert item.observation.knowledge_contamination_status == 'unverified'


@pytest.mark.parametrize('changes', [dict(fact_evidence=()), dict(required_fact_ids=()),
    dict(filing_context_hash='c'*64), dict(share_count_source='unavailable'), dict(raw_output_hash=None),
    dict(fact_evidence=(FactEvidence('shares', date(2024, 1, 1), 'late', HASH),))])
def test_unverified_records_fail_closed(changes):
    result = prepare(records=[valuation(**changes)])
    assert not result.valuations[0].eligible
    assert result.valuations[0].provenance == ProvenanceStatus.UNVERIFIED
    assert result.valuation_mode.value == 'verified_filing_inputs'


def test_explicit_legacy_acceptance_is_never_verified():
    observation = valuation(fact_evidence=(), required_fact_ids=())
    result = prepare(records=[observation], metadata=META | {'valuation_mode': 'legacy_research_inputs'})
    assert result.valuations[0].eligible
    assert result.valuations[0].provenance == ProvenanceStatus.UNVERIFIED
    with pytest.raises(ValueError):
        prepare(records=pd.Series({'2023-01-01': 200.}))


def test_latest_invalidation_or_failed_provenance_suppresses_prior():
    newer = valuation(filed_date=date(2023, 2, 1), effective_available_date=date(2023, 2, 2),
                      fair_value=None, invalidation_reason='failed_dcf')
    for observation in (newer, replace(newer, fair_value=200., invalidation_reason=None, fact_evidence=())):
        result = prepare(records=[observation, valuation()])
        assert result.valuation_asof('A', date(2023, 1, 20)).eligible
        assert not result.valuation_asof('A', date(2023, 2, 2)).eligible


def test_conflicting_duplicates_and_missing_valuations():
    with pytest.raises(ValueError, match='Conflicting'):
        prepare(records=[valuation(), valuation(fair_value=300.)])
    assert prepare(records=[valuation(), valuation()]).content_hash == prepare(records=[valuation()]).content_hash
    missing = prepare()
    assert missing.valuation_asof('A', date(2024, 1, 1)) is None
    assert missing.data_quality['missing_valuation_symbols'] == ['A']


def test_no_network_or_service_imports_in_fresh_process(tmp_path):
    import subprocess
    import sys
    code = '''
import socket, sys
socket.socket.connect = lambda *args: (_ for _ in ()).throw(AssertionError('network'))
from backtesting.primary_inputs import prepare_primary_data
import pandas as pd
idx=pd.date_range('2024-01-02',periods=2)
f=pd.DataFrame(dict(open=[100.,100.],high=[101.,101.],low=[99.,99.],close=[100.,100.],volume=[1.,1.]),index=idx)
prepare_primary_data({'A':f},[],session_index=idx,metadata=dict(calendar_source='observed_union',price_source='synthetic',adjustment_policy='none'))
assert not any(m.startswith(('broker.', 'db.', 'anthropic', 'engine.valuation_engine')) for m in sys.modules)
'''
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_noncanonical_metadata_is_rejected():
    with pytest.raises(ValueError):
        prepare(metadata=META | {'object': object()})
    with pytest.raises(ValueError):
        prepare(metadata=META | {'invalid': float('nan')})


@pytest.mark.parametrize('changes', [dict(effective_available_date=date(2023, 1, 1)),
    dict(fair_value=float('nan')), dict(fair_value=None),
    dict(generated_at=datetime(2024, 1, 1)), dict(knowledge_contamination_status='verified'),
    dict(required_fact_ids=('shares', 'shares'))])
def test_invalid_valuation_contract(changes):
    with pytest.raises(ValueError):
        valuation(**changes)


def test_all_facts_must_be_available_and_bound_to_context():
    late = (FactEvidence('shares', date(2023, 1, 2), 'late', HASH),
            FactEvidence('revenue', date(2022, 12, 31), 'early', HASH))
    assert not prepare(records=[valuation(fact_evidence=late)]).valuations[0].eligible
    wrong_context = (FactEvidence('shares', date(2022, 12, 31), 'early', 'c'*64), late[1])
    assert not prepare(records=[valuation(fact_evidence=wrong_context)]).valuations[0].eligible


def test_policy_and_cost_metadata_are_hashed_without_altering_observations():
    record = valuation()
    original = prepare(records=[record])
    captured = prepare(records=[record], metadata=META | {
        'comparison_settings': {'fee_pct': .01, 'allocation_pct': .1, 'initial_cash': 1000.}})
    assert original.frames == captured.frames
    assert original.valuations == captured.valuations
    assert original.content_hash != captured.content_hash


@pytest.mark.parametrize('dtype', ['complex128', 'bool'])
def test_nonreal_or_boolean_prices_are_not_coerced(dtype):
    frame = bars().astype(dtype)
    with pytest.raises(ValueError, match='numeric'):
        prepare(frame)

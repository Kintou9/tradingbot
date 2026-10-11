"""Hashed local-manifest boundary and fresh-process offline CLI isolation."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backtesting.primary_inputs import canonical_json
from backtesting.primary_reporting import serialize_report
from backtesting.run_primary_backtest import load_prepared_manifest, main
from tests.test_primary_strategy import dataset


def manifest_files(tmp_path):
    data=dataset(symbols=('A',))
    prices={}
    hashes={}
    for s,frame in data.prices_by_symbol.items():
        name=s+'.csv'
        frame[['open','high','low','close','volume']].rename_axis('date').to_csv(tmp_path/name)
        prices[s]=name
        hashes[name]=hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()
    (tmp_path/'valuations.json').write_text(canonical_json([v.observation for v in data.valuations]))
    hashes['valuations.json']=hashlib.sha256((tmp_path/'valuations.json').read_bytes()).hexdigest()
    manifest=dict(schema_version='primary_prepared_input_v1',symbols=list(data.symbols),
        session_dates=[d.isoformat() for d in data.sessions],calendar_source='observed_union',prices=prices,
        valuations_path='valuations.json',source_metadata=dict(price_source='synthetic',adjustment_policy='none'),hashes=hashes)
    path=tmp_path/'input.json'
    path.write_text(json.dumps(manifest))
    return path,manifest


def test_cli_default_strict_and_deterministic_outputs(tmp_path):
    path,_=manifest_files(tmp_path)
    first,second=tmp_path/'first.json',tmp_path/'second.json'
    common=['--input',str(path),'--initial-cash','1000','--allocation','.6']
    assert main(common+['--output',str(first)])==0
    assert main(common+['--output',str(second)])==0
    assert first.read_bytes()==second.read_bytes()
    report=json.loads(first.read_text())
    assert report['config']['valuation_mode']=='verified_filing_inputs'
    assert report['summary']['fill_count']==1 and report['summary']['ending_equity']==1000
    assert str(tmp_path) not in first.read_text()
    before=first.read_bytes()
    with pytest.raises(SystemExit):
        main(common+['--output',str(first)])
    assert first.read_bytes()==before


@pytest.mark.parametrize('engine',['replay','compare'])
def test_cli_baseline_requires_explicit_legacy(tmp_path,engine):
    path,_=manifest_files(tmp_path)
    output=tmp_path/'report.json'
    args=['--input',str(path),'--output',str(output),'--engine',engine,'--initial-cash','1000','--allocation','.6']
    with pytest.raises(SystemExit):
        main(args)
    assert not output.exists()
    assert main(args+['--valuation-mode','legacy_research_inputs'])==0
    report=json.loads(output.read_text())
    baseline=report['baseline'] if engine=='compare' else report
    assert baseline['config']['valuation_mode']=='legacy_research_inputs'
    if engine=='compare':
        assert report['equivalence']['input_data'] is True
        assert report['primary']['data_quality']['valuation_label']=='historically unverified'


@pytest.mark.parametrize('bad',['hash','schema','symbol','mapping','date','path','csv','nan','duplicate_key','missing'])
def test_invalid_manifest_inputs(tmp_path,bad):
    path,manifest=manifest_files(tmp_path)
    if bad=='hash':
        manifest['hashes']['A.csv']='0'*64
    elif bad=='schema':
        manifest['schema_version']='unknown'
    elif bad=='symbol':
        manifest['symbols']=['A','A']
    elif bad=='mapping':
        manifest['prices']={}
    elif bad=='date':
        manifest['session_dates'].append(manifest['session_dates'][-1])
    elif bad=='path':
        manifest['prices']['A']='../A.csv'
        manifest['hashes']['../A.csv']=manifest['hashes'].pop('A.csv')
    elif bad=='csv':
        p=tmp_path/'A.csv'
        p.write_text(p.read_text().replace('volume','invented'))
        manifest['hashes']['A.csv']=hashlib.sha256(p.read_bytes()).hexdigest()
    elif bad=='nan':
        path.write_text('{"schema_version":NaN}')
    elif bad=='duplicate_key':
        path.write_text('{"schema_version":"x","schema_version":"y"}')
    elif bad=='missing':
        manifest['valuations_path']='absent.json'
        manifest['hashes']['absent.json']=manifest['hashes'].pop('valuations.json')
    if bad not in ('nan','duplicate_key'):
        path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError,KeyError,OSError)):
        load_prepared_manifest(path)


@pytest.mark.parametrize('engine',['coupled','replay','compare'])
def test_fresh_process_cli_has_no_provider_or_network_access(tmp_path,engine):
    path,_=manifest_files(tmp_path)
    output=tmp_path/'offline.json'
    args=['--input',str(path),'--output',str(output),'--initial-cash','1000','--allocation','.6','--engine',engine]
    if engine!='coupled':
        args+=['--valuation-mode','legacy_research_inputs']
    code='''
import importlib.abc, socket, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('anthropic','alpaca','db','engine.valuation_engine','engine.price_action_engine.market_data')):
            raise AssertionError('External import: '+fullname)
sys.meta_path.insert(0,Deny())
def deny(*a,**k): raise AssertionError('Network access')
socket.create_connection=deny
socket.socket.connect=deny
from backtesting.run_primary_backtest import main
main(sys.argv[1:])
'''
    result=subprocess.run([sys.executable,'-c',code,*args],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert output.exists()
    report=json.loads(output.read_text())
    assert serialize_report(report)==output.read_text()

"""Cross-source export budgets; no real customer or Windows data."""
import json
import time
import zipfile

import pytest
from chejin_worker_client.diagnostic_bundle import Collector
from test_performance_review_closure import desktop
from test_performance_evidence import monitors, start, samples, wait_until


def test_desktop_receipt_exhaustion_does_not_starve_performance(tmp_path, monkeypatch):
    receipts = tmp_path / 'transactions'
    receipts.mkdir()
    for n in range(405):
        (receipts / f'{n:04}.json').write_text('{"result":"unknown"}')
    perf = tmp_path / 'artifacts/performance'
    perf.mkdir(parents=True)
    name = 'synthetic-000001.jsonl'
    raw = b'{"resources":{"worker":{"rss_bytes":536870912}},"at":1}\n'
    (perf / name).write_bytes(raw)
    (perf / 'index.json').write_text(json.dumps({
        'segments':[{'name':name,'session':'synthetic','first':1,'last':2}],
        'events':[], 'gaps':[], 'sessions':{'synthetic':{'status':'end_unconfirmed'}}}))
    result = desktop(tmp_path, monkeypatch)
    assert 'performance/' + name in result
    assert json.loads(result['performance/' + name])['resources']['worker']['rss_bytes'] == 536870912
    manifest = json.loads(result['收集清单.json'])
    assert any(e['source']=='receipts' and e.get('reason')=='time_or_file_limit' for e in manifest['sources'])
    assert len([n for n in result if n.startswith('receipts/')]) == 400
    assert (perf / name).read_bytes() == raw
    assert len(list(receipts.iterdir())) == 405


@pytest.mark.parametrize('failure', [False, True])
def test_tree_restores_budget_and_shared_pass_stays_bounded(tmp_path, monkeypatch, failure):
    folder = tmp_path/'tasks'; folder.mkdir()
    (folder/'item.json').write_text('{}')
    collector = Collector(tmp_path, tmp_path/'out.zip')
    original = collector.source_end, collector.source_deadline
    if failure:
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic destination failure')
        monkeypatch.setattr(collector, 'file', fail)
    with zipfile.ZipFile(collector.destination,'w') as archive:
        if failure:
            with pytest.raises(RuntimeError): collector.tree(folder,'tasks',archive)
        else: collector.tree(folder,'tasks',archive)
        assert (collector.source_end, collector.source_deadline) == original
        assert collector.tree_usage['tasks'][0] == 1
        collector.tree_usage['tasks'] = (400,0)
        collector.tree(folder,'tasks',archive)
        assert collector.tree_usage['tasks'][0] == 400
        assert (collector.source_end, collector.source_deadline) == original


def test_nested_budget_restores_expired_source_but_not_global_deadline(tmp_path):
    collector=Collector(tmp_path,tmp_path/'out.zip')
    original=collector.source_end,collector.source_deadline
    with collector.source_budget(files=10,seconds=0):
        assert collector.limited()
        with collector.source_budget(files=50,seconds=50):
            assert collector.source_end == 10 and collector.limited()
    assert (collector.source_end,collector.source_deadline) == original
    assert not collector.limited()
    collector.deadline=time.monotonic()-1
    with collector.source_budget(): assert collector.limited()
    assert collector.limited()


def test_actual_sampler_restart_receipts_cap_and_desktop_zip(tmp_path, monitors, monkeypatch):
    # Real resource producers; only the receipt volume is synthetic boundary data.
    first = start(tmp_path, monitors)
    first.close()
    second = start(tmp_path, monitors)
    wait_until(lambda: len({r['session'] for r in samples(tmp_path)}) >= 2)
    second.close()
    source = tmp_path / 'artifacts/performance'
    original = {p.name: p.read_bytes() for p in source.glob('*.jsonl')}
    assert len(original) >= 2
    receipts = tmp_path / 'transactions'
    receipts.mkdir(exist_ok=True)
    for n in range(405):
        (receipts / f'{n:04}.json').write_text('{"result":"unknown"}')
    exported = desktop(tmp_path, monkeypatch)
    assert all('performance/' + name in exported for name in original)
    records = [json.loads(line) for name in original
               for line in exported['performance/' + name].splitlines()]
    assert len({r['session'] for r in records}) >= 2
    assert all('resources' in r for r in records)
    assert {p.name:p.read_bytes() for p in source.glob('*.jsonl')} == original
    # Restore precisely the pre-fix leaked receipt limit: the same real samples
    # disappear despite remaining global budget. This is a regression detector.
    from chejin_worker_client import performance_export
    collect = performance_export.collect
    def leaked(c, archive):
        c.source_end = c.visited
        return collect(c, archive)
    with monkeypatch.context() as patch:
        patch.setattr(performance_export, 'collect', leaked)
        old = desktop(tmp_path, patch)
    assert not any('performance/' + name in old for name in original)

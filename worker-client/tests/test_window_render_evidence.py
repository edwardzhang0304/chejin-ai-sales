import json,time,threading,zipfile
from pathlib import Path
from unittest.mock import patch
from chejin_worker_client.window_render_evidence import WindowRenderEvidence
from chejin_worker_client.diagnostic_bundle import export_bundle


def test_bounded_metadata_export_has_no_content_and_correlation_fields(tmp_path):
    recorder=WindowRenderEvidence(tmp_path,limit=1800)
    for i in range(25):recorder.record('resize','window',width=380,height=720,x=i,visible=True,token='secret',url='secret',text='secret')
    recorder.queue.join();recorder.close()
    paths=list((tmp_path/'artifacts/runtime').glob('window-render*.jsonl'))
    assert len(paths)==2 and all(p.stat().st_size<=1800 for p in paths)
    rows=[json.loads(line) for p in paths for line in p.read_text().splitlines()]
    assert rows and all(r['session']==recorder.session and 'elapsed_ms' in r for r in rows)
    assert all('secret' not in p.read_text() for p in paths)
    z=export_bundle(tmp_path,tmp_path/'evidence.zip')
    with zipfile.ZipFile(z) as bundle:
        assert 'logs/runtime/window-render.jsonl' in bundle.namelist()
        assert 'logs/runtime/window-render.previous.jsonl' in bundle.namelist()


def test_stalled_disk_does_not_block_ui_or_grow_queue(tmp_path):
    gate=threading.Event();entered=threading.Event();original=Path.open
    def stalled(path,*args,**kwargs):
        if path.name=='window-render.jsonl':entered.set();gate.wait(3)
        return original(path,*args,**kwargs)
    with patch.object(Path,'open',stalled):
        recorder=WindowRenderEvidence(tmp_path)
        recorder.record('show','window');assert entered.wait(1)
        started=time.monotonic()
        for _ in range(5000):recorder.record('move','window',x=1)
        assert time.monotonic()-started<.5 and recorder.queue.qsize()<=128 and recorder.dropped>0
        gate.set();recorder.queue.join();recorder.close()
    assert not recorder.thread.is_alive()

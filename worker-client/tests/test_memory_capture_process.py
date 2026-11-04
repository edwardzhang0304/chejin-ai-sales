"""30-second actual observer across normal main and standalone frozen entry.

Synthetic deployment/UI shell; real Qt, sampler process, allocation producer,
request/receipt and export. No Windows/DLL/business end-to-end claim.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

from test_memory_capture_live import wait

ACTOR = Path(__file__).parent / 'helpers/memory_capture_actor.py'


def test_real_main_and_desktop_entry_capture_original_worker(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    with (tmp_path/'worker.log').open('w') as log, (tmp_path/'diagnostic.log').open('w') as diag_log:
        worker = subprocess.Popen([sys.executable, str(ACTOR), 'worker', str(tmp_path)], env=env, stdout=log, stderr=log)
        diagnostic = None
        try:
            wait(lambda: (tmp_path/'actor-ready.json').exists() or worker.poll() is not None, 15)
            assert worker.poll() is None, (tmp_path/'worker.log').read_text()
            diagnostic = subprocess.Popen([sys.executable, str(ACTOR), 'diagnostic', str(tmp_path)], env=env, stdout=diag_log, stderr=diag_log)
            assert diagnostic.wait(60) == 0, (tmp_path/'diagnostic.log').read_text()
            result = json.loads(next(tmp_path.glob('diagnostic-result-*.json')).read_text())
            assert not result['error'] and result['ticks'] > 100
            assert result['max_ui_gap'] < 2, result
            with zipfile.ZipFile(result['result']) as z:
                request = json.loads(z.read('memory/request.json'))
                assert request['status'] == 'finished', request
                report = json.loads(z.read('logs/runtime/python-memory/' + request['report_file']))
                assert report['pid'] == worker.pid != diagnostic.pid
                assert report['request_id'] == request['request_id']
                assert report['capture_id'] == request['capture_id']
                assert report['runtime_session'] == request['target']['runtime_session']
                assert 29 <= report['ended_monotonic'] - report['started_monotonic'] <= 40
                relevant = [site for sample in report['samples'] for site in sample['sites']
                    if any(f['module'] == 'memory_capture_actor.py' for f in site['frames'])]
                assert any(s['baseline_delta_bytes'] > 900000 for s in relevant)
                assert any(s['previous_delta_bytes'] < -900000 for s in relevant)
                assert not any(b'synthetic-no-customer-data' in z.read(n) for n in z.namelist())
                assert json.loads(z.read('memory/collection.json'))['DiySceneStats']['executed_this_request'] is False
        finally:
            (tmp_path/'stop-actor').touch()
            try:worker.wait(5)
            except subprocess.TimeoutExpired:worker.kill(); worker.wait()
            if diagnostic and diagnostic.poll() is None:diagnostic.kill(); diagnostic.wait()

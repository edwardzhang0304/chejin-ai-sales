"""Formal C1 -> Windows factory -> native flow -> artifacts/receipts -> real ZIP.

Only Win32, physical input, clock sleeps and OCR are synthetic. Preparation is
produced by the real formal adapter with a fake backend response. No expected
fault evidence is prewritten; legacy/background receipts are explicit fixtures.
"""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from PIL import Image

from chejin_worker_client import config, failure_evidence
from chejin_worker_client.hook_factory import WindowsC1
from chejin_worker_client.hook_formal_c1 import HookFormalC1
from chejin_worker_client.diagnostic_bundle import export_bundle
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import hook_c1, hook_c1_flow as flow
from apps.wechat_ai_customer_service.tests import test_hook_c1_flow as native_fixtures
FIX=native_fixtures.FIX
from test_failure_evidence_boundaries import home

TASK='00000000-0000-4000-8000-000000000031'
TOKEN='synthetic-lease-token'


class FormalScene:
    def __init__(self, root, monkeypatch, fault='none', cleanup_fault=False):
        self.root=root;self.fault=fault;self.ocr_calls=[];self.grabs=[];self.cleanup_fault=cleanup_fault
        self.boundary=native_fixtures.NativeGuardTests();self.boundary.setUp()
        b=self.boundary;o=b.ops
        self.ops=o;self.dpi=1.0;self.failed=False
        o.ensure_dpi_awareness_status=lambda:{'per_monitor_aware':True}
        o.window_dpi_scale=lambda h:self.dpi
        o.activate_window=Mock(side_effect=self.activate)
        o.hotkey=Mock();o.key_press=Mock()
        o.clipboard_copy=Mock();o.clipboard_read=Mock();o.type_text_with_sendinput_unicode=Mock(return_value={'ok':True})
        o.win32con.VK_CONTROL=17;o.win32con.VK_BACK=8
        o.humanized_input_settings=lambda:{}
        o.try_image_grab=Mock(side_effect=self.grab)
        o.run_ocr=Mock(side_effect=self.ocr)
        self.native=WindowsC1(root/'transactions/hook',artifact_root=root/'artifacts')
        monkeypatch.setattr(self.native,'_window',lambda process:(o,11))
        monkeypatch.setattr(hook_c1,'classify_visible_other_windows',lambda *a:{'blocking_window_details':[]})
        monkeypatch.setattr(flow.time,'sleep',lambda _:None)
        credentials=SimpleNamespace(worker_id='synthetic-worker',client_instance_id='synthetic-instance')
        account={'self_wxid':'synthetic-account','account_epoch':'epoch','process_generation':'generation'}
        client=SimpleNamespace(prepare=lambda credentials,task,token,account:{**account,'task_id':task,
            'lease_fencing_token':token,'preparation_id':'00000000-0000-4000-8000-000000000032',
            'primary_phone':'13800000003','verify_message':'您好我是测试销售','remark_name':'CJH5N28','remark_code':'CJH5N28'})
        self.formal=HookFormalC1(client,credentials,root/'transactions/hook/invites')
        self.preparation=self.formal.prepare(TASK,TOKEN,account)
        self.capture_rows={r['label']:r['rows'] for r in json.loads((FIX/'native-roi-synthetic.json').read_text())}
        # No action/business function is replaced. The synthetic native OCR card
        # says this number is absent, a normal terminal lookup result.
        self.capture_rows['card']=[{'text':'该用户不存在','confidence':.99,'left':10,'right':200,'top':10,'bottom':30}]
        # Production tasks now consume a startup map. Establish it through the
        # real adapter before injecting the task fault; do not bypass the guard.
        from contextlib import nullcontext
        from unittest.mock import patch
        self.fault='none'
        gateway=SimpleNamespace(_locked=lambda *args:nullcontext(SimpleNamespace(raise_if_lost=lambda:None)),
                                _history_identity=lambda *args:None)
        with patch.object(hook_c1,'prepare_window',return_value={'ok':True}):
            self.native.startup(SimpleNamespace(pid=99),gateway,account)
        o.activate_window.reset_mock()  # Assertions below measure task actions after startup.
        self.grabs.clear();self.ocr_calls.clear()
        self.fault=fault

    def activate(self,*args,**kwargs):
        if self.fault=='init_focus':self.boundary.foreground=22
        if self.fault=='init_geometry':self.boundary.geom['left']+=10
        if self.fault=='init_dpi':self.dpi=1.5

    def grab(self,bbox):
        label={659:'header',403:'lookup',489:'card'}[bbox[2]-bbox[0]]
        self.grabs.append(label)
        if self.fault=='before_grab' and label=='lookup':raise PermissionError(13,'secret-do-not-export')
        if self.fault=='after_grab' and label=='lookup':self.boundary.foreground=22
        image=Image.open(FIX/f'native-{label}.png').convert('RGB');image.info['scene_label']=label
        return image

    def ocr(self,image):
        label=image.info['scene_label'];self.ocr_calls.append(label)
        if label=='lookup':
            self.failed=True
            if self.fault=='timeout':raise TimeoutError('secret-do-not-export')
            if self.fault=='permission':raise PermissionError(13,'secret-do-not-export')
            if self.fault=='after_ocr':self.boundary.foreground=22
            if self.fault=='expired':
                now=flow.time.monotonic();self.clock_patch.setattr(flow.time,'monotonic',lambda:now+16)
        elif self.failed and self.cleanup_fault:raise PermissionError(13,'secret-cleanup')
        return copy.deepcopy(self.capture_rows[label])

    def execute(self, monkeypatch):
        self.clock_patch=monkeypatch
        return self.formal.execute_prepared(TASK,TOKEN,self.preparation,
            lambda:self.native.ui(SimpleNamespace(pid=99),self.preparation,Mock()))

    def export(self, name='evidence.zip'):
        path=export_bundle(self.root,self.root/'artifacts/exports'/name)
        with zipfile.ZipFile(path) as z:return {n:z.read(n) for n in z.namelist()}

    def close(self):self.boundary.tearDown()


@pytest.fixture
def scene(home,monkeypatch):
    scenes=[]
    def create(fault='none',cleanup_fault=False):
        s=FormalScene(home,monkeypatch,fault,cleanup_fault);scenes.append(s);return s
    yield create
    for s in scenes:s.close()


def outcome(z):return json.loads(z[f'tasks/{TASK}/diagnostics/outcome.json'])

def original(s):return s.formal.namespace(TASK)


@pytest.mark.parametrize('fault,condition',[('init_focus','foreground_changed'),('init_geometry','main_geometry_changed'),('init_dpi','main_dpi_changed')])
def test_constructor_failure_from_formal_entry_is_exported_and_never_replayed(scene,monkeypatch,fault,condition):
    s=scene(fault)
    with pytest.raises(flow.FlowStop):s.execute(monkeypatch)
    z=s.export();saved=json.loads(z[f'tasks/{TASK}/diagnostics/initialization.json'])
    assert saved['task_id']==saved['flow_id']==TASK
    assert saved['guard_failures'][0]['condition']==condition
    assert saved['exception']['phase']=='initialization'
    assert saved['guard_failures'][0]['expected']['pid']==99
    assert (original(s)/'ui-started.json').exists()
    assert not (original(s)/'outcome.json').exists()
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):s.execute(monkeypatch)
    s.ops.win32api.mouse_event.assert_not_called()
    assert not (original(s)/'submit-attempt.json').exists()


@pytest.mark.parametrize('boundary', ['missing_work_area', 'invalid_work_area', 'undersized_window', 'invalid_dpi'])
def test_startup_contract_failure_keeps_attempt_and_exports_without_input(scene, monkeypatch, boundary):
    s = scene()
    if boundary == 'missing_work_area':
        def unavailable(_):
            raise OSError('synthetic work area unavailable')
        s.ops.screen_work_area = unavailable
    elif boundary == 'invalid_work_area':
        s.ops.screen_work_area = lambda _: {'left': 0, 'top': 0, 'width': 0, 'height': 0}
    elif boundary == 'undersized_window':
        s.boundary.geom['width'] = 600
    else:
        s.dpi = float('nan')
    expected_error = OSError if boundary == 'missing_work_area' else flow.FlowStop
    with pytest.raises(expected_error):
        s.execute(monkeypatch)
    z = s.export()
    saved = json.loads(z[f'tasks/{TASK}/diagnostics/initialization.json'])
    assert saved['exception']['phase'] == 'initialization'
    assert saved['task_id'] == TASK
    before = {p: p.read_bytes() for p in original(s).glob('*.json')}
    assert (original(s) / 'ui-started.json').exists()
    with pytest.raises(ValueError, match='PREVIOUS_ATTEMPT'):
        s.execute(monkeypatch)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    s.ops.win32api.mouse_event.assert_not_called()
    s.ops.type_text_with_sendinput_unicode.assert_not_called()
    assert not (original(s) / 'submit-attempt.json').exists()


@pytest.mark.parametrize('fault,kind',[('timeout','TimeoutError'),('permission','PermissionError')])
def test_real_ocr_exception_and_cleanup_exception_have_distinct_safe_causes(scene,monkeypatch,fault,kind):
    s=scene(fault,cleanup_fault=True);r=s.execute(monkeypatch);z=s.export();r=outcome(z)
    assert r['error_code']=='C1_ACTION_FAILED'
    assert r['exception']['exception_type']==kind
    assert r['cleanup_exception']['exception_type']=='PermissionError'
    assert r['exception']['operation']=='ocr:lookup'
    assert r['cleanup_exception']['phase']=='cleanup'
    assert r['exception']['frames'] and r['task_id']==TASK
    assert not any(b'secret-do-not-export' in b or b'secret-cleanup' in b for b in z.values())
    assert not r['submit_attempted'] and not (original(s)/'submit-attempt.json').exists()


@pytest.mark.parametrize('fault,screenshot,ocr',[('before_grab','not_produced','not_produced'),
    ('after_grab','saved','not_produced'),('timeout','saved','not_produced'),
    ('after_ocr','saved','saved'),('expired','saved','saved')])
def test_actual_capture_stages_export_existing_material_without_authorizing_action(scene,monkeypatch,fault,screenshot,ocr):
    s=scene(fault);r=s.execute(monkeypatch)
    # Restore synthetic time before bounded exporter, without affecting the flow result.
    if fault=='expired':monkeypatch.undo()
    z=s.export();r=outcome(z)
    material=next(x for x in r['evidence']['frames'] if x['label']=='lookup')
    assert (material['screenshot'],material['ocr'])==(screenshot,ocr)
    assert material['action_authorized'] is False
    if screenshot=='saved':assert f'tasks/{TASK}/screenshots/02-lookup.png' in z
    if ocr=='saved':assert json.loads(z[f'tasks/{TASK}/ocr/02-lookup.json'])
    assert s.ocr_calls.count('lookup')==(0 if fault in {'before_grab','after_grab'} else 1)
    assert not r['submit_attempted'] and not (original(s)/'submit-attempt.json').exists()


def test_normal_lookup_uses_native_flow_and_keeps_evidence_authorization_separate(scene,monkeypatch):
    s=scene();r=s.execute(monkeypatch);z=s.export()
    assert r['status']=='not_found' and r['cleanup']=='search_empty_verified'
    assert all(f['action_authorized'] for f in r['evidence']['frames'])
    assert not r['evidence']['failures']
    assert outcome(z)['status']=='not_found'
    assert not (original(s)/'submit-attempt.json').exists()


@pytest.mark.parametrize('fault,recorder_fails',[('permission',False),('disk',False),('permission',True)])
def test_diagnostic_write_failures_preserve_formal_outcome_and_mark_export_partial(scene,monkeypatch,fault,recorder_fails):
    s=scene('after_ocr');write=Path.write_text
    def denied(path,*args,**kwargs):
        if path.parent.name=='diagnostics' and path.name=='outcome.json':
            if fault=='disk':raise OSError(28,'secret-disk')
            raise PermissionError(13,'secret-permission')
        return write(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',denied)
    if recorder_fails:monkeypatch.setattr('chejin_worker_client.c1_diagnostics.record_capture_failure',Mock(side_effect=OSError(28,'secret')))
    r=s.execute(monkeypatch);before=(original(s)/'outcome.json').read_bytes();z=s.export()
    assert r['error_code']=='WINDOW_FOCUS_OR_GEOMETRY_CHANGED'
    assert r['evidence']['failures'][0]['target']=='diagnostics/outcome.json'
    assert (original(s)/'outcome.json').read_bytes()==before
    assert json.loads(z['收集清单.json'])['partial']
    assert f'tasks/{TASK}/diagnostics/outcome.json' not in z
    assert any(b'foreground_changed' in b for n,b in z.items() if n.startswith('receipts/'))
    assert not (original(s)/'submit-attempt.json').exists()


@pytest.mark.parametrize('count',[405,1000])
@pytest.mark.parametrize('fault',['init_focus','after_ocr'])
def test_current_failed_task_survives_history_flood_with_original_proofs(scene,monkeypatch,count,fault):
    s=scene(fault)
    if fault=='init_focus':
        with pytest.raises(flow.FlowStop):s.execute(monkeypatch)
    else:s.execute(monkeypatch)
    for i in range(count):
        p=s.root/f'artifacts/tasks/old-{i:04d}/diagnostics/outcome.json';p.parent.mkdir(parents=True)
        p.write_text('{"status":"old"}')
    # Active task is deliberately older; recovery journal priority beats mtime.
    os.utime(s.root/f'artifacts/tasks/{TASK}',(1,1))
    before={p: p.read_bytes() for p in original(s).glob('*.json')}
    z=s.export()
    expected='initialization' if fault=='init_focus' else 'outcome'
    assert f'tasks/{TASK}/diagnostics/{expected}.json' in z
    if fault=='after_ocr':
        assert f'tasks/{TASK}/screenshots/02-lookup.png' in z
        assert f'tasks/{TASK}/ocr/02-lookup.json' in z
    assert any('ui-started.json' in n for n in z)
    assert json.loads(z['收集清单.json'])['partial']
    assert any(e['status']=='omitted' and e['source']=='tasks' for e in json.loads(z['收集清单.json'])['sources'])
    assert all(p.read_bytes()==raw for p,raw in before.items())


def test_constructor_diagnostic_and_fallback_failure_still_exposes_missing_material(scene,monkeypatch):
    s=scene('init_focus');write=Path.write_text
    def denied(path,*args,**kwargs):
        if path.parent.name=='diagnostics':raise PermissionError(13,'private detail')
        return write(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',denied)
    monkeypatch.setattr('chejin_worker_client.c1_diagnostics.record_capture_failure',Mock(side_effect=OSError(28,'private detail')))
    with pytest.raises(flow.FlowStop,match='WINDOW_FOCUS'):s.execute(monkeypatch)
    z=s.export();manifest=json.loads(z['收集清单.json'])
    assert manifest['partial']
    assert any(e.get('reason')=='required_c1_diagnostics_missing' and e['task_id']==TASK for e in manifest['sources'])
    assert (original(s)/'ui-started.json').exists()
    assert not (original(s)/'outcome.json').exists()


@pytest.mark.parametrize('placement',['tail','middle'])
def test_real_failure_log_with_torn_line_keeps_good_lines_and_safe_member_isolation(scene,monkeypatch,placement):
    s=scene('after_ocr');write=Path.write_text
    def denied(path,*args,**kwargs):
        if path.parent.name=='diagnostics' and path.name=='outcome.json':raise PermissionError(13,'private exception')
        return write(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',denied)
    s.execute(monkeypatch)
    log=s.root/'artifacts/runtime/evidence-recorder-failures.jsonl'
    valid=log.read_bytes()
    torn=b'{"token":"do-not-export-secret",\n'
    raw=valid+torn+(valid if placement=='middle' else b'')
    log.write_bytes(raw)
    # Malformed first archive member must not swallow a later valid member.
    incident=s.root/'incidents/old.zip';incident.parent.mkdir()
    with zipfile.ZipFile(incident,'w') as z:
        z.writestr('bad.json',b'{"token":"do-not-export-secret",')
        z.writestr('events.jsonl',raw)
        z.writestr('last.json',b'{"event":"last_survives"}')
    archive_before=incident.read_bytes();z=s.export()
    rows=z['logs/runtime/evidence-recorder-failures.jsonl'].splitlines()
    assert len(rows)==(1 if placement=='tail' else 2)
    assert all(json.loads(row)['event']=='evidence_capture_failed' for row in rows)
    assert 'incidents/legacy/old/last.json' in z
    assert 'incidents/legacy/old/events.jsonl' in z
    assert not any(b'do-not-export-secret' in b or b'private exception' in b for b in z.values())
    sources=json.loads(z['收集清单.json'])['sources']
    assert any(e.get('reason')=='invalid_jsonl_line' and e.get('line')==2 for e in sources)
    assert any(e['source'].endswith('!/bad.json') and e['status']=='read_failed' for e in sources)
    assert log.read_bytes()==raw and incident.read_bytes()==archive_before


@pytest.mark.parametrize('material',['screenshot','ocr'])
def test_capture_write_failure_keeps_original_error_and_unusable_frame(scene,monkeypatch,material):
    s=scene();write=Path.write_text;save=Image.Image.save
    def denied_text(path,*args,**kwargs):
        if path.name=='02-lookup.json':raise OSError(28,'private storage detail')
        return write(path,*args,**kwargs)
    def denied_image(image,path,*args,**kwargs):
        if Path(path).name=='02-lookup.png':raise PermissionError(13,'private storage detail')
        return save(image,path,*args,**kwargs)
    if material=='ocr':monkeypatch.setattr(Path,'write_text',denied_text)
    else:monkeypatch.setattr(Image.Image,'save',denied_image)
    r=s.execute(monkeypatch);z=s.export()
    assert r['error_code']=='C1_ACTION_FAILED'
    assert r['exception']['exception_type']==('OSError' if material=='ocr' else 'PermissionError')
    frame=next(f for f in r['evidence']['frames'] if f['label']=='lookup')
    assert not frame['action_authorized']
    assert frame[material]=='produced_not_saved'
    assert r['evidence']['failures']
    assert json.loads(z['收集清单.json'])['partial']
    assert not (original(s)/'submit-attempt.json').exists()


def test_unwritable_original_journal_never_enters_ui(scene,monkeypatch):
    s=scene();open_file=Path.open
    def denied(path,*args,**kwargs):
        if path.name=='ui-started.json':raise OSError(28,'private disk detail')
        return open_file(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',denied)
    with pytest.raises(OSError):s.execute(monkeypatch)
    s.ops.activate_window.assert_not_called()
    assert not s.grabs and not s.ocr_calls
    assert not (original(s)/'outcome.json').exists()


def test_invalid_evidence_metadata_does_not_drop_other_log_lines(scene,monkeypatch):
    s=scene('after_ocr');s.execute(monkeypatch)
    log=s.root/'artifacts/runtime/malformed.jsonl';log.parent.mkdir(exist_ok=True)
    log.write_text('{"evidence":{"failures":4}}\n{"event":"survives"}\n')
    z=s.export()
    assert len(z['logs/runtime/malformed.jsonl'].splitlines())==2
    assert any(e.get('reason')=='invalid_evidence_failures' for e in json.loads(z['收集清单.json'])['sources'])


@pytest.mark.parametrize('destination_failure',[False,True])
def test_desktop_main_exports_real_fault_chain_with_responsive_status(scene,monkeypatch,destination_failure):
    s=scene('after_ocr');s.execute(monkeypatch)
    before={p:p.read_bytes() for p in original(s).glob('*.json')}
    # Separate process keeps a real Qt loop isolated from older tests' Qt stubs.
    code=r'''
import sys,time,json,zipfile
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from chejin_worker_client import diagnostic_ui as ui
from chejin_worker_client.diagnostic_bundle import Collector
app=QApplication([])
root=Path(sys.argv[1]);fail=sys.argv[2]=='True'
ui.default_data_root=lambda:root
ui.DiagnosticDialog.open_folder=lambda self:None
original=Collector.file
def slow(self,*args,**kwargs):
    time.sleep(.015) # Only slow the storage boundary; collection remains real.
    return original(self,*args,**kwargs)
Collector.file=slow
if fail:
    original_write=zipfile.ZipFile.writestr
    def denied(self,*args,**kwargs):raise OSError(28,'private disk detail')
    zipfile.ZipFile.writestr=denied
ticks=[];deadline=time.monotonic()+15;result={}
timer=QTimer()
def poll():
    ticks.append(time.monotonic())
    dialogs=[w for w in app.topLevelWidgets() if isinstance(w,ui.DiagnosticDialog)]
    if time.monotonic()>deadline:app.exit(2);return
    if not dialogs:return
    d=dialogs[0]
    if d.job.running:
        assert not d.button.isEnabled()
        assert not d.job.start()
        return
    if not d.button.isEnabled():return # queued completion not delivered yet
    assert len(ticks)>5
    if fail:
        assert '导出未完成' in d.label.text()
        assert not d.folder_button.isEnabled()
        assert not list((root/'artifacts/exports').glob('*.zip'))
        assert not list((root/'artifacts/exports').glob('*.tmp'))
    else:
        assert d.partial and '部分证据缺失' in d.label.text()
        assert d.folder_button.isEnabled()
        with zipfile.ZipFile(d.result_path) as z:
            p='tasks/'+sys.argv[3]
            assert p+'/ocr/02-lookup.json' in z.namelist()
            assert p+'/screenshots/02-lookup.png' in z.namelist()
            report=json.loads(z.read(p+'/diagnostics/outcome.json'))
            assert report['guard_failures'][0]['condition']=='foreground_changed'
        assert len(list((root/'artifacts/exports').glob('*.zip')))==1
    result.update(ticks=len(ticks),destination_failure=fail,status_verified=True)
    app.quit()
timer.timeout.connect(poll);timer.start(5)
assert ui.main()==0
assert result
print(json.dumps(result))
'''
    subprocess.run([sys.executable,'-c',code,str(s.root),str(destination_failure),TASK],
                   check=True,timeout=25,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'})
    assert all(p.read_bytes()==raw for p,raw in before.items())


@pytest.mark.parametrize('history_count',[24,405,1000])
def test_capacity_and_directory_limits_preserve_current_fallback_before_optional_history(scene,monkeypatch,history_count):
    from chejin_worker_client.diagnostic_bundle import Collector, update_root
    s=scene('init_focus');write=Path.write_text
    def denied(path,*args,**kwargs):
        if path.parent.name=='diagnostics':raise PermissionError(13,'private diagnosis denied')
        return write(path,*args,**kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(Path,'write_text',denied)
        with pytest.raises(flow.FlowStop,match='WINDOW_FOCUS'):s.execute(monkeypatch)
    fallback=s.root/'artifacts/runtime/evidence-recorder-failures.jsonl'
    assert 'foreground_changed' in fallback.read_text()
    # Background update/incident metadata are fixtures; the actual failure and
    # fallback above are produced by the real formal execution chain.
    updates=update_root(s.root)/'control';updates.mkdir(parents=True,exist_ok=True)
    update=updates/'update-result.json';update.write_text('{"stage":"verify","ok":false}')
    incident=s.root/'artifacts/incidents/current.json';incident.parent.mkdir(parents=True)
    incident.write_text('{"task_id":"'+TASK+'","event":"current_fault"}')
    before={p:p.read_bytes() for p in [fallback,update,incident,*original(s).glob('*.json')]}
    for i in range(history_count):
        p=s.root/f'artifacts/tasks/old-{i:04d}/diagnostics/outcome.json';p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'payload':'x'*65536,'status':'old'}))
    # Small configurable budget exercises exactly the default capacity branch;
    # essential facts fit, optional historical payloads cannot all fit.
    collector=Collector(s.root,s.root/'limited.zip',max_bytes=200000)
    path=collector.run()
    with zipfile.ZipFile(path) as z:
        assert b'foreground_changed' in z.read('logs/runtime/evidence-recorder-failures.jsonl')
        assert 'logs/update/control/update-result.json' in z.namelist()
        assert 'incidents/current/current.json' in z.namelist()
        assert any(n.endswith('/ui-started.json') for n in z.namelist())
        manifest=json.loads(z.read('收集清单.json'))
        assert manifest['partial']
        assert any(e.get('reason')=='total_size_limit' and e['source'].startswith('tasks/') for e in manifest['sources'])
        assert len([n for n in z.namelist() if n.startswith('tasks/old-')])<history_count
    assert collector.tree_usage['tasks'][0]<=400
    assert collector.visited<=3000
    assert all(p.read_bytes()==raw for p,raw in before.items())

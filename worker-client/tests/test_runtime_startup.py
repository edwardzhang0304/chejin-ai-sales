"""Real Qt startup/event loops; runtime I/O, health and WebEngine rendering controlled.

Native WebEngine crashes in this Mac's offscreen platform before the test can
complete. Its renderer alone is replaced; both production window constructors,
run_app entrypoints, startup methods and QTimer scheduling execute unchanged.
"""
import json,os,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
PROGRAM=r'''
import importlib,json,sys
from pathlib import Path
from unittest.mock import patch,Mock
from contextlib import nullcontext
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.update_coordinator import UpdateCoordinator
from chejin_worker_client import post_update_health,update_diagnostics
module=importlib.import_module('chejin_worker_client.'+sys.argv[1])
mode=sys.argv[2];bound=sys.argv[3]=='bound';events=[];windows=[]
clsname='WorkerWindow' if sys.argv[1]=='ui' else 'WorkerWebWindow'
original=getattr(module,clsname)
class Renderer(QWidget):
    def page(self):return Mock()
    def load(self,url):events.append(['local_asset',url.isLocalFile()])
renderer=patch.object(module,'QWebEngineView',Renderer) if sys.argv[1]=='web_ui' else nullcontext()
class Window(original):
    def __init__(self,**kwargs):
        events.append(['construct',kwargs['auto_start_runtime']]);super().__init__(**kwargs);windows.append(self)
        if mode=='normal':QTimer.singleShot(20,self.close)
    def closeEvent(self,event):events.append(['close']);super().closeEvent(event)
class Gate:
    def __init__(self,plan,token):self.count=0
    def observe(self,snapshot):
        events.append(['observe',snapshot]);self.count+=1
        if mode=='failure':raise RuntimeError('controlled unhealthy runtime')
        if mode=='pending' and self.count==1:return None
        QTimer.singleShot(10,windows[0].close);return Path('controlled-marker')
def start(self,binding):events.append(['start',binding.worker_id])
def reconcile(self):events.append(['reconcile'])
def snapshot(self):return {'ready':True,'binding_state':'bound','startup_failures':[],'required_threads':['controlled'],'threads':{'controlled':True}}
def diagnostic(*args,**kwargs):events.append(['diagnostic',kwargs['phase']])
context=None if mode=='normal' else {'mode':'updated','plan':{'healthy_marker_path':str(Path(sys.argv[4])/'healthy.json')},'token':'local-test-only'}
with renderer,patch.object(module,clsname,Window),patch.object(module,'load_binding',return_value=Binding('worker','token','instance',run_status='paused') if bound else None),patch.object(module,'take_update_startup_context',return_value=context),patch.object(TaskRunner,'start',start),patch.object(TaskRunner,'post_update_runtime_health_snapshot',snapshot),patch.object(UpdateCoordinator,'start_result_reconciliation',reconcile),patch.object(post_update_health,'RuntimeHealthGate',Gate),patch.object(update_diagnostics,'record_update_startup_failure',diagnostic):
    code=module.run_app()
    windows[0].start_runtime_services()
assert code==0 and len(windows)==1
assert sum(e[0]=='start' for e in events)==int(bound),events
assert sum(e[0]=='reconcile' for e in events)==1,events
assert events[0]==['construct',mode=='normal'],events
observes=[e for e in events if e[0]=='observe']
assert len(observes)==(0 if mode=='normal' else 2 if mode=='pending' else 1),events
for e in observes:
    assert e[1]['ready'] and e[1]['ui_event_loop_alive']
    assert events.index(e)>next(i for i,v in enumerate(events) if v[0]=='reconcile')
assert sum(e[0]=='diagnostic' for e in events)==int(mode=='failure'),events
assert any(e[0]=='close' for e in events),events
print(json.dumps({'mode':mode,'bound':bound,'events':events,'controlled_boundaries':['runtime I/O','health response']+(['WebEngine renderer'] if sys.argv[1]=='web_ui' else [])}))
'''
@pytest.mark.parametrize('ui',['ui','web_ui'])
@pytest.mark.parametrize('mode,bound',[('normal',True),('normal',False),('updated',True),('updated',False),('pending',True),('failure',True)])
def test_real_ui_runtime_startup(tmp_path,ui,mode,bound):
    script=tmp_path/'run_ui.py';script.write_text(PROGRAM,encoding='utf-8')
    env={**os.environ,'PYTHONPATH':str(ROOT),'CHEJIN_WORKER_HOME':str(tmp_path/'worker-home'),'CHEJIN_OBSERVABILITY_ENABLED':'false','QT_QPA_PLATFORM':'offscreen','QTWEBENGINE_CHROMIUM_FLAGS':'--no-sandbox --disable-gpu','PYTHONDONTWRITEBYTECODE':'1'}
    p=subprocess.run([sys.executable,str(script),ui,mode,'bound' if bound else 'unbound',str(tmp_path)],env=env,cwd=ROOT,text=True,capture_output=True,timeout=25)
    (tmp_path/'stdout.log').write_text(p.stdout);(tmp_path/'stderr.log').write_text(p.stderr)
    assert p.returncode==0,p.stderr+'\n'+p.stdout
    evidence=json.loads(p.stdout.splitlines()[-1]);(tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))

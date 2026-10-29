"""Only Hook heartbeat UTC presentation changes; OCR/log formatting is retained."""
import ast,os,time
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from chejin_worker_client.models import Binding,WorkerProfile
from chejin_worker_client import startup_check_state as checks
from test_web_ui_binding_behavior import _headless_web_ui_module,WebUiBindingBehaviorTest

@pytest.fixture
def local_shanghai():
 if not hasattr(time,'tzset'):pytest.skip('Local timezone test requires tzset')
 old=os.environ.get('TZ');os.environ['TZ']='Asia/Shanghai';time.tzset()
 try:yield
 finally:
  if old is None:os.environ.pop('TZ',None)
  else:os.environ['TZ']=old
  time.tzset()

def formatter(name):
 source=Path(__file__).parents[1]/'chejin_worker_client'/name
 node=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='_format_time')
 scope={'datetime':datetime,'timezone':timezone};exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
 return scope['_format_time']

@pytest.mark.parametrize('name',['web_ui.py','ui.py'])
@pytest.mark.parametrize('stamp',['2026-10-13T01:19:09','2026-10-13T01:19:09+00:00','2026-10-13T01:19:09Z'])
def test_hook_heartbeat_utc_formats_same_local_time(name,stamp,local_shanghai):
 f=formatter(name);assert f(stamp,backend_utc=True)=='09:19:09'

@pytest.mark.parametrize('name',['web_ui.py','ui.py'])
def test_naive_ocr_time_formatting_remains_unchanged(name,local_shanghai):
 f=formatter(name);assert f('2026-10-13T01:19:09')=='01:19:09'


def test_real_worker_model_only_hook_heartbeat_gets_utc_normalization(local_shanghai):
 with _headless_web_ui_module() as module:
  with patch.object(module,'_log_rows',lambda:[]),patch.object(module,'latest_incident',lambda:None),patch.object(module,'lock_summary',lambda:{}):
   w=WebUiBindingBehaviorTest._window(module,Binding('worker','synthetic','client','faulted'))
   w._publish=Mock();w.profile=WorkerProfile('worker','测试销售','faulted',last_heartbeat_at='2026-10-13T01:19:09',rpa_component_status='ready',wechat_status='logged_in')
   w.runner.hook_services=SimpleNamespace(startup_check_state=checks.passed)
   assert w._state()['model']['status']['lastHeartbeat']=='09:19:09'
   w.runner.hook_services=None
   assert w._state()['model']['status']['lastHeartbeat']=='01:19:09'


@pytest.mark.parametrize('name',['web_ui.py','ui.py'])
def test_hook_heartbeat_is_beijing_even_if_os_zone_is_utc(name,monkeypatch):
 if not hasattr(time,'tzset'):pytest.skip('tzset unavailable')
 old=os.environ.get('TZ');monkeypatch.setenv('TZ','UTC');time.tzset()
 try:
  assert formatter(name)('2026-10-13T01:19:09Z',backend_utc=True)=='09:19:09'
  assert formatter(name)('2026-10-13T01:19:09')=='01:19:09'
 finally:
  if old is None:os.environ.pop('TZ',None)
  else:os.environ['TZ']=old
  time.tzset()

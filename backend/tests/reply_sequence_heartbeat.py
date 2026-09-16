"""Provide the normal HTTP heartbeat omitted by the direct-TaskRunner harness.

No database timestamp writes, state repair, task/receipt creation or authority
override. Read guards and the 120-second offline gate remain unchanged.
"""
import json
import threading
import time
import pytest


@pytest.fixture(autouse=True)
def live_test_worker_heartbeat(http_api, monkeypatch, tmp_path):
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client.api import WorkerApiClient
    from chejin_worker_client import storage
    from chejin_worker_client.ui_lock import lock_summary
    original = TaskRunner.__init__
    stopped = threading.Event()
    threads, events = [], []
    def initialized(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def run():
            client = None
            try:
                while not stopped.wait(1 if client is None else 15):
                    binding = getattr(self, 'binding', None)
                    if binding is None: continue
                    if client is None: client = WorkerApiClient(self.api.base_url)
                    control = storage.load_runtime_control()
                    lock = lock_summary()
                    task = getattr(self, 'current_task', None)
                    try:
                        response = client.heartbeat(binding,
                            running_status='running' if control.get('inflight_flow_id') or task else 'idle',
                            current_task=getattr(task, 'id', None),
                            rpa_component_status='ready', wechat_status='logged_in',
                            current_step=getattr(self, 'current_step', None),
                            local_lock_summary={**lock,'capabilities':{'reply_sequence_version':1}})
                        events.append({'monotonic':time.monotonic(),'worker_id':binding.worker_id,'ok':True})
                    except Exception as exc:
                        events.append({'monotonic':time.monotonic(),'worker_id':binding.worker_id,'ok':False,'error':repr(exc)})
            finally:
                if client: client.session.close()
        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread); thread.start()
    monkeypatch.setattr(TaskRunner, '__init__', initialized)
    yield
    stopped.set()
    for thread in threads: thread.join(15)
    (tmp_path/'heartbeat.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
    assert not any(thread.is_alive() for thread in threads)
    assert events and all(event['ok'] for event in events), events

"""Isolated Worker process; backend/model/Hook simulators live in the driver.

Real production coordinator, receiver, durable inbox, sender, history and ACK.
Physical Windows observations alone are supplied by the synthetic test fixture.
No backend database, model, pytest, or test driver is imported into this process.
"""
import functools
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import tracemalloc
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

root = Path(sys.argv[1]).resolve()
os.environ['CHEJIN_WORKER_HOME'] = str(root)
from PySide6.QtCore import QCoreApplication, QTimer
import psutil
from chejin_worker_client import storage, memory_capture as mc, performance_evidence as pe
from chejin_worker_client.performance_contract import POLICY
from chejin_worker_client.hook_config import HookDeployment
from chejin_worker_client.hook_process import HookProcessSnapshot
from chejin_worker_client.hook_gateway import HookGateway
from chejin_worker_client.hook_backend import HookIdentityClient, HookWorkerCredentials, HookHeartbeatState
from chejin_worker_client.hook_connection import HookConnectionManager
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_voice import HookVoiceTranscriber, capture_voice_audio
from chejin_worker_client.hook_sender import HookNativeSender
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
# The GUI constructs TaskRunner before users can request live diagnostics.
# Construct it here too: importing it lazily at the first send misrepresents startup.
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.python_memory_profile import Profile

wire = sys.stdout
sys.stdout = sys.stderr
app = QCoreApplication([])
event_file = (root / 'observer.jsonl').open('w', buffering=1)
event_lock = threading.Lock()
event_count = 0
measurement_started = None
last_pulse = None
max_gap = 0.
last_trace_state = None
measure = False
process = psutil.Process()
monitor = coordinator = manager = runtime = runner = dispatcher = None
voice = HookVoiceTranscriber(max_audio_bytes=4096, max_text_bytes=2048)
finished_profile_ref = None

def respond(value):
    wire.write(json.dumps(value) + '\n'); wire.flush()

def record(kind, **fields):
    global event_count
    if not measure: return
    with event_lock:
        if event_count >= 20000: raise AssertionError('test observer volume exceeded')
        event_file.write(json.dumps({'kind': kind, 'monotonic': time.monotonic(), **fields})+'\n')
        event_count += 1

def observe(owner, name):
    original = getattr(owner, name)
    @functools.wraps(original)
    def call(*args, **kwargs):
        active, start, tracing = measure, time.monotonic(), tracemalloc.is_tracing()
        try: return original(*args, **kwargs)
        finally:
            if active:
                extra = {}
                if owner is HookFormalClient and name == 'call':
                    extra = {'method': args[2], 'path': args[3]}
                record('span', operation=owner.__name__+'.'+name, start=start,
                    seconds=time.monotonic()-start, tracing_at_start=tracing,
                    tracing_at_end=tracemalloc.is_tracing(), **extra)
            if owner is not Profile: pe.emit('loop', name='business_actor')
    setattr(owner, name, call)

for owner, names in (
    (HookFormalClient, ('call', 'native_send_settle', 'observe_send')),
    (HookGateway, ('_response',)), (NativeInbox, ('persist',)),
    (NativeProjectionStore, ('project_next',)), (HookDeliveryOutbox, ('mark_stored',)),
    (HookNativeSender, ('execute',)),
    (HookBusinessDispatcher, ('tick', '_recover_send')),
    (Profile, ('sample', 'finish')),
    (HookFormalRuntime, ('sync', 'drain', 'reply_once', 'settle_retained_send', 'finish'))):
    for name in names: observe(owner, name)

def pulse():
    global last_pulse, max_gap, last_trace_state
    pe.emit('ui')  # Real Qt timer; not a Windows/WebEngine rendering claim.
    if measure:
        now = time.monotonic()
        if last_pulse is not None: max_gap = max(max_gap, now-last_pulse)
        last_pulse = now
        tracing = tracemalloc.is_tracing()
        record('resource', rss=process.memory_info().rss,
               cpu_seconds=sum(process.cpu_times()[:2]), tracing=tracing)
        if tracing != last_trace_state:
            record('tracer_transition_observed', tracing=tracing)
            last_trace_state = tracing
timer = QTimer(); timer.timeout.connect(pulse); timer.start(50)

def drive(binding):
    history = runtime.sync(binding)
    assert history['history_ready'], history
    batch_id = history['message_batch']['batch_id']
    deadline = time.monotonic() + 30  # Same formal integration driver deadline.
    sends = []
    while time.monotonic() < deadline:
        data = runtime.batch_view(batch_id)
        if data.get('processing'):
            runtime.wait(.05); continue
        action = data.get('reply_action') or {}
        if action.get('status') != 'queued':
            return {'decision': data.get('decision'), 'sends': sends, 'batch_id': batch_id}
        history = runtime.sync(binding, show_progress=False)
        result = runtime.reply_once(binding, {**data, 'batch_id': batch_id}, history=history)
        if result:
            sends.append(result)
            if result['status'] in ('confirmation_pending', 'unknown_send_result'):
                return {'decision': result['status'], 'sends': sends, 'batch_id': batch_id}
        runtime.wait(.05)
    raise AssertionError('production reply entry did not settle within original 30 second driver budget')

def commands():
    global monitor, coordinator, manager, runtime, runner, dispatcher, measure, measurement_started, finished_profile_ref
    try:
        for line in sys.stdin:
            value = json.loads(line)
            op = value['op']
            if op == 'init':
                deployment = HookDeployment(**value['deployment'])
                snapshot = value['process']
                for key in ('command_arguments', 'listener_owners'): snapshot[key] = tuple(snapshot[key])
                snapshot = HookProcessSnapshot(**snapshot)
                gateway = HookGateway(deployment, timeout_seconds=1, max_response_bytes=2*1024*1024,
                    lock_timeout_seconds=.1, inspector=lambda _: snapshot)
                credentials = HookWorkerCredentials(**value['credentials'])
                identity = HookIdentityClient(value['base_url'], 5, 65536)
                manager = HookConnectionManager(gateway, identity,
                    NativeInbox(max_body_bytes=4096, max_inbox_bytes=65536),
                    receiver_timeout_seconds=1, on_persistence_failure=lambda: (_ for _ in ()).throw(AssertionError('persistence')))
                prepared = manager.prepare(credentials, HookHeartbeatState('idle','ready','logged_in',None,None,'{}'))
                prepared.clock.observe(prepared.account)
                runtime = HookFormalRuntime(HookFormalClient(value['base_url'], 5), credentials,
                    gateway, prepared, root / 'transactions' / 'hook')
                runner = TaskRunner(WorkerApiClient(value['base_url']), None,
                    on_profile=lambda _: None, on_status=lambda _: None, on_step=lambda _: None,
                    on_task=lambda _: None, on_result=lambda _: None, on_error=lambda _: None)
                runner.binding = Binding(**value['credentials'], run_status=value.get('run_status','paused'))
                if runner.binding.run_status=='running':
                    dispatcher = HookBusinessDispatcher(runtime.client, root/'transactions'/'hook', None, None,
                        automatic_transcription_disabled=lambda: True)
                # Observe actual waits on both direct and dispatcher-created
                # runtimes. Keep random delays and their wait function intact.
                def observe_wait(target):
                    original_wait = target.wait
                    def observed_wait(seconds):
                        started = time.monotonic()
                        result = original_wait(seconds)
                        record('wait', requested_seconds=seconds, start=started,
                               seconds=time.monotonic()-started)
                        return result
                    target.wait = observed_wait
                    return target
                observe_wait(runtime)
                if dispatcher is not None:
                    original_factory = dispatcher.runtime_factory
                    dispatcher.runtime_factory = lambda *a, **kw: observe_wait(original_factory(*a, **kw))
                (root / 'runtime').mkdir(exist_ok=True)
                (root / 'runtime/session.json').write_text(json.dumps({
                    'session_id': uuid.uuid4().hex, 'pid': os.getpid(), 'status': 'running'}))
                monitor = pe.install(root, policy=POLICY)
                pe.register_process('wechat', snapshot.pid, snapshot.started_at)
                coordinator = mc.install(root, monitor)
                deadline = time.monotonic() + POLICY.sample_seconds * 2 + 5
                while monitor.capture_pressure() is not None and time.monotonic() < deadline: time.sleep(.05)
                assert monitor.capture_pressure() is None, monitor.capture_pressure()
                respond({'pid': os.getpid(), 'callback_url': deployment.callback_url, 'policy': POLICY.payload(), 'account':runtime.account})
            elif op == 'arm':
                measure = True
                measurement_started = time.monotonic()
                record('measurement_start')
                respond({'started': measurement_started})
            elif op == 'await_capture':
                deadline = time.monotonic()+5
                while not tracemalloc.is_tracing() and time.monotonic()<deadline: time.sleep(.01)
                assert tracemalloc.is_tracing()
                respond({'tracing': True})
            elif op == 'reply':
                runtime.drain(value['binding'])
                started = time.monotonic()
                respond({**drive(value['binding']), 'business_seconds': time.monotonic()-started})
            elif op == 'dispatch':
                assert runner.binding.run_status=='running' and dispatcher is not None
                services = SimpleNamespace(connection=manager, _credentials=runtime.credentials,
                    startup_window_ready_for=lambda _: True)  # Synthetic physical window is ready.
                started=time.monotonic(); deadline=started+30
                before={'run_status':runner.binding.run_status, 'can_start':runner._can_start_new_flow()}
                while time.monotonic()<deadline:
                    dispatcher.tick(runner,services)
                    receipts={p.stem:json.loads(p.read_bytes())
                        for p in (root/'transactions/hook/send-receipts').glob('*.json')}
                    control=storage.load_runtime_control()
                    if len(receipts)==value['expected_sends'] and not control['inflight_flow_id']:
                        respond({'before':before,'run_status':runner.binding.run_status,
                            'can_start':runner._can_start_new_flow(), 'receipts':receipts,
                            'local_control':control, 'business_seconds':time.monotonic()-started,
                            'tracing':tracemalloc.is_tracing()})
                        break
                    runner.stop_event.wait(.05)
                else: raise AssertionError('formal dispatcher did not settle within 30 seconds')
            elif op == 'sync':
                respond(runtime.sync(value['binding']))
            elif op == 'voice_save':
                audio = capture_voice_audio(runtime.scope, value['binding']['peer_wxid'],
                    value['native_id'], max_audio_bytes=4096)
                assert audio is not None
                saved = voice.transcribe_once(runtime.gateway, runtime.prepared.process, audio,
                    automatic_transcription_disabled=lambda: True)
                respond(saved)
            elif op == 'voice_publish':
                saved = voice.saved_parent(runtime.scope, value['binding']['peer_wxid'], value['native_id'])
                respond(runtime.client.voice_result(runtime.credentials, runtime.account, value['binding'], saved))
            elif op == 'replay_settlement':
                respond(runtime.client.call(runtime.credentials, 'POST',
                    f"/workers/{runtime.credentials.worker_id}/wechat/native-replies/{value['action_id']}/settle",
                    value['request'], task_id=value['task_id'], token=value['token'], flow_id=value['flow_id']))
            elif op == 'clock':
                # Test-only controlled clock for original reminder due policy.
                from chejin_worker_client import hook_history_sync, hook_backend, hook_sender
                stamp = datetime.fromisoformat(value['now'])
                anchor = time.monotonic()
                def now(): return stamp + timedelta(seconds=time.monotonic()-anchor)
                class ControlledWallClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        current=now()
                        return current.astimezone(tz) if tz else current.replace(tzinfo=None)
                # Both sides of the original call-window validation advance
                # together. The validation itself and monotonic budgets stay.
                hook_history_sync.datetime=ControlledWallClock
                hook_sender.datetime=ControlledWallClock
                # Calendar-jump tests advance the heartbeat monotonic domain
                # by the same amount. Capture/UI/wait timers stay real, and
                # clock jump, RTT, lease and send guards run unchanged.
                shift=(stamp-datetime.now(timezone.utc)).total_seconds()
                clock_tick=lambda:time.monotonic()+shift
                hook_backend.time=SimpleNamespace(**{**vars(time), 'monotonic':clock_tick,
                    'time':lambda:time.time()+shift})
                runtime.clock.monotonic=clock_tick
                respond({'now': now().isoformat()})
            elif op == 'capture_resources':
                profile = coordinator.profile
                state = {'tracing':tracemalloc.is_tracing(),
                    'capture_id':profile.identity['capture_id'],
                    'profile_alive':profile.thread.is_alive(),
                    'baseline_released':profile.baseline is None,
                    'previous_released':profile.previous is None,
                    'threads':sorted(t.name for t in threading.enumerate()),
                    'fd_count':process.num_fds(),
                    'prior_profile_released':finished_profile_ref is None or finished_profile_ref() is None}
                assert not state['tracing'] and not state['profile_alive']
                finished_profile_ref = weakref.ref(profile)
                respond(state)
            elif op == 'inventory':
                respond(runtime.client.call(runtime.credentials, 'POST',
                    f'/workers/{runtime.credentials.worker_id}/wechat/native-work',
                    {**runtime.account, 'retained_flow_id': runtime.active_flow_id}))
            elif op == 'recover':
                proofs = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (root / 'transactions/hook/sends').rglob('*.json')}
                dispatcher = HookBusinessDispatcher(runtime.client, root/'transactions'/'hook', None, None,
                    automatic_transcription_disabled=lambda: True)
                services = SimpleNamespace(connection=manager, _credentials=runtime.credentials,
                    startup_window_ready_for=lambda _: False)  # No new UI task in recovery.
                # The dispatcher constructs its own runtime, adopts the original
                # Flow, settles its send and finishes on the next tick.
                for _ in range(3): dispatcher.tick(runner, services)
                assert proofs and all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
                    for name,digest in proofs.items())
                receipts = {p.stem: json.loads(p.read_bytes())
                    for p in (root/'transactions/hook/send-receipts').glob('*.json')}
                respond({'receipts': receipts, 'original_hashes': proofs,
                    'local_control': storage.load_runtime_control()})
            elif op == 'report':
                # Called only after the requester observes a terminal result.
                if coordinator.profile and coordinator.profile.thread:
                    coordinator.profile.thread.join(3)
                    assert not coordinator.profile.thread.is_alive()
                assert not tracemalloc.is_tracing()
                record('measurement_end')
                measure = False
                event_file.flush()
                respond({'pid': os.getpid(), 'observer_file':str(root/'observer.jsonl'),
                    'event_count':event_count, 'measurement_started':measurement_started,
                    'measurement_ended':time.monotonic(), 'qt_max_gap':max_gap,
                    'backend_imported':'app.core.database' in sys.modules, 'tracing':False})
            elif op == 'close':
                break
            else: raise AssertionError(op)
    except BaseException as exc:
        respond({'error': traceback.format_exc(), 'code':getattr(exc,'code',None)})
    finally:
        if coordinator: coordinator.close()
        if coordinator and coordinator.profile and coordinator.profile.thread: coordinator.profile.thread.join(3)
        if monitor: monitor.close()
        if manager: manager.close()
        event_file.close()
        app.quit()

thread = threading.Thread(target=commands, name='synthetic-business-driver', daemon=True)
thread.start()
app.exec()
thread.join(5)

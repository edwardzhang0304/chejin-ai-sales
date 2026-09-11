"""Installation evidence must reject lost bindings or rewritten original facts."""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('pending_install_gate', ROOT/'worker-client/scripts/run-windows-pending-read-install.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def original():
    return {'binding': {'id':1, 'worker_id':'test-worker', 'worker_token':'synthetic', 'client_instance_id':'original', 'run_status':'paused'},
            'settings': [('accept_schedule', '{"enabled":true}', 'original-time')],
            'outbox': [{'outbox_id':'original-outbox', 'read_run_id':'original-flow', 'conversation_id':'original-customer',
                        'payload_json':'{"contract_revision":"0.9.75","evidence":"original bytes"}', 'status':'waiting'}]}


def test_confirmed_delivery_and_stopped_fault_status_preserve_identity(original):
    after = copy.deepcopy(original)
    after['binding']['run_status'] = 'faulted'
    after['outbox'][0]['status'] = 'confirmed'
    gate.assert_identity(original, after)


@pytest.mark.parametrize('change', ['token','instance','customer','flow','payload','delete','schedule'])
def test_data_damage_cannot_pass_install_gate(original, change):
    after = copy.deepcopy(original)
    if change == 'token': after['binding']['worker_token'] = 'changed'
    elif change == 'instance': after['binding']['client_instance_id'] = 'new-binding'
    elif change == 'customer': after['outbox'][0]['conversation_id'] = 'different-customer'
    elif change == 'flow': after['outbox'][0]['read_run_id'] = 'new-flow'
    elif change == 'payload': after['outbox'][0]['payload_json'] = '{"contract_revision":"0.9.77"}'
    elif change == 'delete': after['outbox'].clear()
    elif change == 'schedule': after['settings'].clear()
    with pytest.raises(AssertionError): gate.assert_identity(original, after)


def test_fixture_child_output_decodes_utf8_independent_of_windows_locale():
    import subprocess, sys
    result = subprocess.run([sys.executable, '-c', "import sys;sys.stdout.buffer.write('原流程恢复'.encode('utf-8'))"],
                            text=True, encoding='utf-8', capture_output=True, check=True)
    assert result.stdout == '原流程恢复'
    source = (Path(__file__).resolve().parents[3] / 'worker-client/scripts/run-windows-pending-read-install.py').read_text()
    assert 'text=True, encoding="utf-8", capture_output=True' in source


def test_expected_fault_transition_updates_time_but_preserves_binding(original):
    original['binding']['updated_at'] = '2026-09-11T12:00:00+00:00'
    after = copy.deepcopy(original)
    after['binding'].update(run_status='faulted', updated_at='2026-09-11T12:00:01+00:00')
    gate.assert_identity(original, after)
    assert gate.binding_difference(original, after)['changed_binding_fields'] == ['run_status', 'updated_at']
    after['binding']['worker_token'] = 'changed'
    with pytest.raises(AssertionError, match='Binding identity changed'):
        gate.assert_identity(original, after)


@pytest.mark.parametrize('status,stamp', [('paused','2026-09-11T12:00:01+00:00'), ('faulted','2026-09-11T11:59:59+00:00'), ('running','2026-09-11T12:00:01+00:00')])
def test_unexpected_status_or_timestamp_still_blocks(original,status,stamp):
    original['binding']['updated_at'] = '2026-09-11T12:00:00+00:00'
    after = copy.deepcopy(original); after['binding'].update(run_status=status,updated_at=stamp)
    with pytest.raises(AssertionError): gate.assert_identity(original,after)

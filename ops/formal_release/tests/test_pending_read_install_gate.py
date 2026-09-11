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

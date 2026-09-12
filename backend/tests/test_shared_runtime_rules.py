"""Fixed semantic controls across the actual backend/Worker/Sidecar entry points."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'worker-client'))
from app.contracts import c2 as backend_contract
from app.contracts.shared_rules import shared_adapter
from app.services import message_contract as backend_message
from app.services.c3_service import _normalize_voice_duration
from chejin_worker_client import c2_contract as worker_contract
from chejin_worker_client import message_contract as worker_message
from chejin_worker_client.api import ApiError
from chejin_worker_client.pre_send_checkpoint import _normalize_voice_duration_value
from chejin_worker_client.transaction_outcomes import classify_outbox_recovery
from chejin_worker_client.shared_rules import contract_rules, message_contract
from apps.wechat_ai_customer_service.adapters.business_viewport_continuity import _normalized_voice_duration


@pytest.mark.parametrize('value,expected', [
    ('6秒', '6'), (' 6.250 Seconds ', '6.25'), (6, '6'),
    ('1.2346sec', '1.235'), ('0.001s', '0.001'), ('1e1', '10'),
    (None, ''), ('', ''), ('语音', ''), ('0秒', ''), (-1, ''),
])
def test_voice_duration_uses_one_unchanged_rule(value, expected):
    for normalize in (_normalize_voice_duration, _normalize_voice_duration_value, _normalized_voice_duration):
        assert normalize(value) == expected


@pytest.mark.parametrize('value,reply,identity', [
    ('混动\n车型', '混动 车型', '混动车型'),
    ('混动 车型', '混动 车型', '混动 车型'),
    ('\t hello\n world \u00a0', 'hello world', 'hello world'),
    ('您好，\r\n请问', '您好， 请问', '您好，请问'),
    ('SUV\n车型', 'SUV 车型', 'SUV车型'),
    (None, '', ''),
])
def test_ocr_identity_and_send_hash_remain_distinct(value, reply, identity):
    for module in (backend_message, worker_message, message_contract):
        assert module.canonical_reply_text(value) == reply
        assert module.canonical_message_identity_text(value) == identity
        assert module.reply_text_hash(value) == hashlib.sha256(reply.encode()).hexdigest()
    assert worker_message.reply_text_hash('混动 车型') != worker_message.reply_text_hash('混动车型')


def test_shipped_contract_is_unchanged_and_entrypoints_use_same_source():
    raw = json.loads((ROOT / 'contracts/c2_contract_v3.json').read_text())
    expected = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for module in (backend_contract, worker_contract):
        assert module.contract_sha256() == expected
        assert module.contract_revision() == raw['contract_revision']
        assert module.contract_values('row_kinds') == frozenset(raw['row_kinds'])
        assert module.contract_row_rules() == raw['row_rules']
        assert module.image_contract() == raw['image_contract']
    assert Path(shared_adapter('contract_rules').__file__).resolve() == Path(contract_rules.__file__).resolve()
    assert Path(shared_adapter('message_contract').__file__).resolve() == Path(message_contract.__file__).resolve()


@pytest.mark.parametrize('mutation,method,args,error', [
    ({'row_kinds': None}, 'contract_values', ('row_kinds',), 'Invalid C2 contract list'),
    ({'row_rules': None}, 'contract_row_rules', (), 'Invalid C2 contract row_rules'),
    ({'row_rules': {'text_bubble': []}}, 'contract_row_rules', (), 'Invalid C2 row rule'),
    ({'row_rules': {}}, 'contract_row_rules', (), 'row_rules and row_kinds'),
    ({'ingestible_row_kinds': []}, 'contract_row_rules', (), 'ingestible_row_kinds and row_rules'),
    ({'image_contract': None}, 'image_contract', (), 'Invalid C2 contract image_contract'),
    ({'sample': []}, 'contract_value_map', ('sample',), 'Invalid C2 contract map'),
    ({'sample': {'bad': 1}}, 'contract_value_map', ('sample',), 'Invalid C2 contract map values'),
    ({'contract_revision': ''}, 'contract_revision', (), 'Invalid C2 contract revision'),
])
def test_malformed_contracts_still_fail_at_both_entrypoints(monkeypatch, mutation, method, args, error):
    payload = copy.deepcopy(backend_contract.c2_contract_v3())
    payload.update(mutation)
    for module in (backend_contract, worker_contract):
        monkeypatch.setattr(module, 'c2_contract_v3', lambda: payload)
        with pytest.raises(RuntimeError, match=error):
            getattr(module, method)(*args)


@pytest.mark.parametrize('value,errors', [
    ({'score': 0.5}, []),
    ({'score': 'bad'}, ["$.score: 'bad' is not of type 'number'"]),
    ({'score': float('nan')}, ['$.score: non-finite number']),
    ({'score': float('inf')}, ['$.score: non-finite number']),
    ({'score': [float('-inf')]}, ['$.score[0]: non-finite number', "$.score: [-inf] is not of type 'number'"]),
])
def test_image_schema_validation_and_nonfinite_guards(monkeypatch, value, errors):
    payload = {'image_contract': {'schemas': {'probe': {
        'type': 'object', 'required': ['score'], 'properties': {'score': {'type': 'number'}}}}}}
    for module in (backend_contract, worker_contract):
        monkeypatch.setattr(module, 'c2_contract_v3', lambda: payload)
        assert module.validate_image_result_schema(value, 'probe') == errors
        with pytest.raises(RuntimeError, match='Invalid C2 image schema'):
            module.validate_image_result_schema(value, 'missing')


@pytest.mark.parametrize('status', [200, 400, 401, 408, 409, 413, 425, 429, 500, 502, 503, 504])
def test_backend_and_worker_share_code_priority_and_http_fallback(status):
    payload = backend_contract.c2_contract_v3()['outbox_recovery_contract']
    groups = [('identity_quarantined_codes', 'identity_quarantined'),
              ('refresh_and_rebuild_codes', 'refresh_and_rebuild'),
              ('split_and_retry_codes', 'split_and_retry'),
              ('target_terminated_codes', 'target_terminated'),
              ('conversation_terminated_codes', 'conversation_terminated'),
              ('capability_paused_codes', 'capability_paused')]
    for field, expected in groups:
        for code in payload[field]:
            assert backend_contract.recovery_action_for_error(code, status) == expected
            assert classify_outbox_recovery(ApiError(code, 'ignored', status)) == expected
    expected = 'retry' if status in (408, 425, 429) or status >= 500 else 'capability_paused'
    assert backend_contract.recovery_action_for_error('UNRECOGNIZED', status) == expected
    assert classify_outbox_recovery(ApiError('UNRECOGNIZED', 'ignored', status)) == expected
    # A valid explicit action takes precedence even when the status/code would retry/quarantine.
    assert classify_outbox_recovery(ApiError('MESSAGE_IDENTITY_COLLISION', '', status, {'recovery_action': 'capability_paused'})) == 'capability_paused'
    assert classify_outbox_recovery(ApiError('UNRECOGNIZED', '', status, {'recovery_action': 'invalid'})) == expected


@pytest.mark.parametrize('layout', ['source', 'packaged'])
def test_shared_rules_load_from_isolated_distributed_runtime(tmp_path, layout):
    """Use production imports in a copied runtime, with no repository on sys.path."""
    import os
    import shutil
    import subprocess
    package = tmp_path / layout
    package.mkdir()
    shutil.copytree(ROOT / 'worker-client/chejin_worker_client', package / 'chejin_worker_client', ignore=shutil.ignore_patterns('__pycache__'))
    contract_dir = package / 'contracts' if layout == 'packaged' else package.parent / 'contracts'
    shutil.copytree(ROOT / 'contracts', contract_dir)
    target = package / 'omniauto-rpa/apps/wechat_ai_customer_service/adapters'
    target.mkdir(parents=True)
    for name in ('contract_rules.py', 'message_contract.py'):
        shutil.copy2(ROOT / 'worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters' / name, target / name)
    # The updater must remain importable without loading the RPA/UI adapter stack.
    script = '''
import json
from chejin_worker_client.c2_contract import contract_sha256
from chejin_worker_client.message_contract import canonical_message_identity_text
from chejin_worker_client.pending_read_recovery import package_recovery_capability
import chejin_worker_client.chejin_updater
assert canonical_message_identity_text('混动\\n车型') == '混动车型'
print(json.dumps({'sha': contract_sha256(), 'recovery': package_recovery_capability()}))
'''
    env = {**os.environ, 'PYTHONPATH': str(package), 'CHEJIN_WORKER_HOME': str(tmp_path / 'state')}
    result = subprocess.run([sys.executable, '-c', script], cwd=package, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['sha'] == backend_contract.contract_sha256()

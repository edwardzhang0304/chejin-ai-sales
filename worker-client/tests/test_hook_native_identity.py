"""Synthetic native identities across ingress, durable delivery and upgrade evidence."""
from dataclasses import replace
import hashlib
import json

import pytest
from jsonschema import Draft7Validator
from chejin_worker_client import storage
from chejin_worker_client.shared_rules import hook_contract_rules as rules
from chejin_worker_client.hook_contacts import resolve_friend_shortcode
from chejin_worker_client.hook_outbox import HookDeliveryOutbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from test_hook_outbox import setup, project, message, NOW
from test_hook_facts import callback, incoming, history, history_row
from test_hook_gateway import lab

VALID = ['wxid_sample_A', 'nativeuser2468', 'Legacy-Sales_42', 'abcdef', 'A' * 64]
INVALID = [None, 12345678901, True, '', '12345678901', ' abcdef', 'abcdef ',
           'abcdef\n', 'abcde', 'A'*65, 'wxid_', 'WXID_test', 'wxid_bad-name',
           'a@chatroom', 'user@openim', 'gh_example', 'GH_example', 'filehelper',
           'FILEHELPER', 'weixin', 'newsapp', 'notification_messages', '顾客甲',
           "user'123", 'user/123', 'user\\123', 'user\x00123']

@pytest.mark.parametrize('value', VALID + INVALID)
def test_shared_native_identity_and_json_contract_agree(value):
    expected = value in VALID
    assert rules.wxid(value) is expected
    schema={'type':'string','pattern':rules.NATIVE_PRIVATE_ID_PATTERN}
    assert Draft7Validator(schema).is_valid(value) is expected

@pytest.mark.parametrize('peer',VALID[:3])
def test_unique_contact_callback_and_history_use_identical_native_id(peer):
    candidate=resolve_friend_shortcode({'friend':[{'wxid':peer,'markname':'CJTEST42'}]},'CJTEST42','wxid_sales_test')
    assert candidate.matching_objects==1 and candidate.candidate_wxid==peer
    live=callback(incoming(fromid=peer))[0]
    past=history([history_row(StrTalker=peer)])[0]
    assert live.disposition==past.disposition=='message_candidate'
    assert live.peer_wxid==past.peer_wxid==peer
    assert resolve_friend_shortcode({'friend':[{'wxid':peer,'markname':'CJTEST42'},
        {'wxid':peer,'markname':'CJTEST42'}]},'CJTEST42','wxid_sales_test').candidate_wxid is None
    assert resolve_friend_shortcode({'wxid':peer,'account':'different_alias','markname':'CJTEST42'},
        'CJTEST42','wxid_sales_test').candidate_wxid==peer
    assert resolve_friend_shortcode({'wxid':peer,'username':'conflicting_user','markname':'CJTEST42'},
        'CJTEST42','wxid_sales_test').candidate_wxid is None

@pytest.mark.parametrize('peer',VALID[1:3])
def test_native_identity_outbox_restart_keeps_exact_bytes_and_rejects_other_peer(setup,peer):
    _,scope,receive=setup
    scope=replace(scope,peer_wxid=peer)
    original=project(receive,[{**message(receive),'fromid':peer}])
    out=HookDeliveryOutbox(max_storage_bytes=1048576);out.enqueue_projection(original['receive_seq'],scope,now=NOW)
    item=out.claim_next(scope,now=NOW,lease_seconds=1)
    assert item.envelope['peer_wxid']==peer
    before=item.payload_json
    recovered=HookDeliveryOutbox(max_storage_bytes=1048576).claim_next(scope,now='2026-10-09T12:00:02+00:00',lease_seconds=10)
    assert recovered.payload_json==before
    wrong=replace(scope,peer_wxid='other_native_user')
    with pytest.raises(RuntimeError,match='HOOK_OUTBOX_BINDING_CHANGED'):
        out.claim_next(wrong,now='2026-10-09T12:00:13+00:00',lease_seconds=10)
    assert out.mark_stored(recovered,rules.receipt(recovered.envelope),now=NOW)

@pytest.mark.parametrize('quarantined',[False,True])
def test_v2_projection_remains_immutable_and_readable_after_upgrade(setup,quarantined):
    _,scope,receive=setup
    data=project(receive)
    data['contract_version']='hook-observation-projection-v2'
    if quarantined:
        data['observations'][0].update(disposition='quarantined',peer_wxid=None,content=None,
            reason_code='HOOK_PRIVATE_IDENTITY_REQUIRED')
    encoded=rules.canonical(data);digest=hashlib.sha256(encoded.encode()).hexdigest()
    with storage.db_connection() as db:
        db.execute('UPDATE hook_observation_projection SET contract_version=?,projection_json=?,projection_digest=?',
            (data['contract_version'],encoded,digest));db.commit()
    assert NativeProjectionStore(max_projection_bytes=1048576,max_entries=200).project_next(receive) is None
    out=HookDeliveryOutbox(max_storage_bytes=1048576);out.enqueue_projection(data['receive_seq'],scope,now=NOW)
    item=out.claim_next(scope,now=NOW,lease_seconds=10)
    assert (item is None) is quarantined
    with storage.db_connection() as db:
        row=db.execute('SELECT projection_json,projection_digest FROM hook_observation_projection').fetchone()
        assert tuple(row)==(encoded,digest)

@pytest.mark.parametrize('native_self',VALID[1:3])
def test_login_and_heartbeat_keep_native_self_identity_exactly(lab,native_self):
    server,gateway,process,scope,*_=lab
    server.wxid=native_self
    assert gateway.login_status(process).self_wxid==native_self
    from chejin_worker_client.hook_backend import HookHeartbeatIdentity
    assert HookHeartbeatIdentity(native_self,process.generation,gateway.deployment.revision,
        process.dll_sha256).payload()['self_wxid']==native_self


def test_published_contract_identity_patterns_and_source_match():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    contract = json.loads((root/'contracts/hook_contract_v1.json').read_text())
    assert contract['rules_source_sha256'] == hashlib.sha256((root/contract['rules_source']).read_bytes()).hexdigest()
    checked = []
    def walk(value):
        if isinstance(value, dict):
            for key, field in value.get('properties', {}).items():
                if key in {'self_wxid', 'peer_wxid', 'from_wxid', 'to_wxid'}:
                    assert field['pattern'] == rules.NATIVE_PRIVATE_ID_PATTERN
                    checked.append(key)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(contract)
    assert len(checked) == 11


def test_projection_cannot_claim_another_version_even_with_recomputed_digest(setup):
    _, scope, receive = setup
    data = project(receive)
    data['contract_version'] = 'hook-observation-projection-v2'
    encoded = rules.canonical(data)
    with storage.db_connection() as db:
        db.execute('UPDATE hook_observation_projection SET projection_json=?,projection_digest=?',
            (encoded, hashlib.sha256(encoded.encode()).hexdigest())); db.commit()
    with pytest.raises(RuntimeError, match='HOOK_OUTBOX_PROJECTION_CHANGED'):
        HookDeliveryOutbox(max_storage_bytes=1048576).enqueue_projection(data['receive_seq'],scope,now=NOW)

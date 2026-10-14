import json,sqlite3
from dataclasses import replace
from pathlib import Path
import pytest
from chejin_worker_client.release_transport import selected_release_channel
from chejin_worker_client.release_package_contract import validate_release_contract,verify_staged_package,ClientUpdateError
from chejin_worker_client.shared_rules import client_release_rules as rules
from test_client_update import _signed_release,_archive


def selection(root,mode):
    root.mkdir(exist_ok=True)
    with sqlite3.connect(root/'worker_client.sqlite3') as db:
        db.execute('CREATE TABLE client_settings(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO client_settings VALUES (?,?)',('wechat_transport_v1',json.dumps(mode)))


def test_explicit_mode_selects_its_channel_and_never_creates_database(tmp_path):
    assert selected_release_channel(tmp_path,build_root=tmp_path)=='gray'
    assert not (tmp_path/'worker_client.sqlite3').exists()
    selection(tmp_path,{'mode':'hook'})
    assert selected_release_channel(tmp_path,build_root=tmp_path)=='gray-hook'
    (tmp_path/'runtime-build-identity.json').write_text(json.dumps({'transport':'ocr'}))
    with pytest.raises(ClientUpdateError,match='更新模式'):selected_release_channel(tmp_path,build_root=tmp_path)

@pytest.mark.parametrize('mode',[{}, {'mode':'invalid'}, {'mode':'hook','extra':True}, None])
def test_invalid_selection_fails_closed(tmp_path,mode):
    selection(tmp_path,mode)
    with pytest.raises(ClientUpdateError):selected_release_channel(tmp_path,build_root=tmp_path)

@pytest.mark.parametrize('transport,channel',[('hook','gray'),('ocr','gray-hook')])
def test_signed_channel_does_not_accept_opposite_transport(transport,channel):
    assert not rules.compatible_package({'transport':transport,'channel':channel},channel)


def test_native_requires_explicit_channel_and_new_updater_compatibility():
    payload,digest=_archive();release,_=_signed_release(payload,digest)
    native=replace(release,channel='gray-hook',minimum_updater_version='0.10.1')
    with pytest.raises(ClientUpdateError):validate_release_contract(native)
    validate_release_contract(native,expected_channel='gray-hook')
    with pytest.raises(ClientUpdateError):validate_release_contract(replace(native,minimum_updater_version='0.9.69'),expected_channel='gray-hook')
    with pytest.raises(ClientUpdateError):validate_release_contract(release,expected_channel='gray-hook')
    assert rules.compatible_package({},'gray') and not rules.compatible_package({},'gray-hook')


def test_compiled_native_profile_selects_native_even_before_binding(tmp_path):
    (tmp_path/'runtime-build-identity.json').write_text(json.dumps({'transport':'hook'}))
    assert selected_release_channel(tmp_path,build_root=tmp_path)=='gray-hook'
    assert not (tmp_path/'worker_client.sqlite3').exists()
    (tmp_path/'runtime-build-identity.json').write_text(json.dumps({'transport':''}))
    with pytest.raises(ClientUpdateError):selected_release_channel(tmp_path,build_root=tmp_path)

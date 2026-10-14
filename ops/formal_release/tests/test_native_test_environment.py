import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('native_test_environment',Path(__file__).parents[1]/'native_test_environment.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
URI='postgresql+psycopg://postgres@127.0.0.1:18831/hook_test'


def test_native_selection_is_preserved_and_database_scopes_do_not_share_state(tmp_path,monkeypatch):
    monkeypatch.setenv('HOOK_TEST_POSTGRES_URL',URI)
    backend=['tests/test_hook_business_http.py','tests/test_hook_gray_multiworker.py',
             'tests/test_hook_fault_recovery_http.py','tests/test_ai_capacity.py']
    worker=['tests/test_hook_dispatcher.py','tests/test_task_runner.py']
    groups=module.execution_groups([('backend',backend),('worker-client',worker)],database_parent=tmp_path)
    assert {node for g in groups for node in g['nodes']}=={'backend/'+n for n in backend}|{'worker-client/'+n for n in worker}
    assert len(groups)==5 and all(g['cwd']=='.' for g in groups)
    environments=[g['env'] for g in groups]
    assert len({e['CHEJIN_WORKER_HOME'] for e in environments})==5
    assert len({e['DATABASE_URL'] for e in environments})==5
    for g in groups:
        database=g['env']['DATABASE_URL']
        if any('gray_multiworker' in n for n in g['nodes']):assert database.endswith('/hook_gray_test')
        elif any('fault_recovery_http' in n for n in g['nodes']):assert database.endswith('/chejin_hook_gray_recovery_test')
        elif any('ai_capacity' in n for n in g['nodes']):assert database==URI
        else:assert database.startswith('sqlite:///') and '/test-native-' in database
    again=module.execution_groups([('backend',backend)],database_parent=tmp_path)
    assert environments[0]['CHEJIN_WORKER_HOME']!=again[0]['env']['CHEJIN_WORKER_HOME']


@pytest.mark.parametrize('uri', ['',URI.replace('127.0.0.1','192.168.50.10'),
    URI.replace('/hook_test','/production'),URI.replace('18831','5432'),
    URI.replace('postgres@','postgres:secret@'),URI+'?sslmode=disable',URI+'#extra',
    URI.replace('postgresql+psycopg','postgresql')])
def test_unknown_or_remote_database_is_rejected_before_test_home_creation(tmp_path,monkeypatch,uri):
    monkeypatch.setenv('HOOK_TEST_POSTGRES_URL',uri)
    with pytest.raises(ValueError,match='HOOK_ISOLATED_POSTGRES_REQUIRED'):
        module.execution_groups([('.', ['backend/tests/test_ai_capacity.py'])],database_parent=tmp_path)
    assert not list(tmp_path.iterdir())


def test_default_environment_does_not_supply_a_database_implicitly(tmp_path,monkeypatch):
    monkeypatch.delenv('HOOK_TEST_POSTGRES_URL',raising=False)
    with pytest.raises(ValueError):module.execution_groups([],database_parent=tmp_path)

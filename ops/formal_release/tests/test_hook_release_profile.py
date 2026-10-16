import importlib.util,os
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('native_release_profiles',ROOT/'ops/formal_release/release_profiles.py')
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)


def test_native_branch_is_explicit_and_old_ocr_default_stays(monkeypatch):
    monkeypatch.delenv('CHEJIN_RELEASE_TRANSPORT',raising=False)
    assert profile.source_branch()=='codex/gray-release-0.9.x'
    monkeypatch.setenv('CHEJIN_RELEASE_TRANSPORT','hook')
    assert profile.source_branch()=='gray-release-0.10.x'
    monkeypatch.setenv('CHEJIN_RELEASE_TRANSPORT','invalid')
    with pytest.raises(ValueError,match='RELEASE_TRANSPORT_INVALID'):profile.source_branch()


def test_native_requires_all_its_tests_and_never_reuses_ocr_handoff():
    with pytest.raises(ValueError,match='HOOK_SOURCE_TEST_PLAN_INCOMPLETE'):
        profile.validate_native_source_plan({'pytest':[{'cwd':'.','nodes':['worker-client/tests/test_client_update.py']}]},ROOT)
    with pytest.raises(ValueError,match='HOOK_REQUIRES_FRESH_SOURCE_TESTS'):
        profile.validate_native_source_plan({'reviewed_handoff':{'status':'passed'}},ROOT)
    tests=[p.relative_to(ROOT).as_posix() for base in ('backend/tests','worker-client/tests') for p in (ROOT/base).glob('test_hook*.py')
           if p.relative_to(ROOT).as_posix() not in profile.PRIVATE_PACKAGE_CASES]
    tests+=['worker-client/tests/test_task_runner.py','worker-client/tests/test_client_update.py','worker-client/tests/test_update_coordinator.py','worker-client/tests/test_chejin_updater.py','backend/tests/test_ai_capacity.py']
    profile.validate_native_source_plan({'pytest':[{'cwd':'.','nodes':tests}]},ROOT)


def test_native_workflow_requires_review_and_cannot_publish():
    import yaml
    path=ROOT/'.github/workflows/hook-windows-build.yml'
    source=path.read_text();workflow=yaml.safe_load(source)
    triggers=workflow.get('on',workflow.get(True))
    assert set(triggers)=={'workflow_dispatch'}
    assert triggers['workflow_dispatch']['inputs']['approved']['default'] is False
    assert workflow['permissions']=={'contents':'read','actions':'read'}
    assert '-Transport hook' in source and 'HOOK_REVIEWED_BUILD_APPROVAL_REQUIRED' in source
    assert 'source_evidence.py resolve' in source
    assert not any(word in source for word in ('-SkipTests','-SkipPreflight','sign-client-release','publish_staged','FORMAL_SSH_KEY'))
    evidence=yaml.safe_load((ROOT/'.github/workflows/release-evidence.yml').read_text())
    native=evidence['jobs']['native-source']
    assert native['runs-on']=='ubuntu-latest' and 'postgres' in native['services']
    assert native['env']['HOOK_TEST_POSTGRES_URL'].endswith('/hook_test')


def test_native_branch_agrees_with_confirmed_architecture_and_all_consumers(monkeypatch):
    import yaml
    document=(ROOT/'deliverables/AI智能客服售前跟进系统_Hook技术方案_v0.10.4_2026-10-16.md').read_text()
    section=document.split('## 14. 发布验收与变更约束',1)[1].split('## 15.',1)[0]
    import re
    branch=re.search(r'目标分支：`([^`]+)`',section).group(1)
    monkeypatch.setenv('CHEJIN_RELEASE_TRANSPORT','hook')
    assert profile.source_branch()==branch
    evidence=yaml.safe_load((ROOT/'.github/workflows/release-evidence.yml').read_text())
    triggers=evidence.get('on',evidence.get(True))
    assert branch in triggers['push']['branches']
    assert "github.ref_name == '"+branch+"'" in evidence['jobs']['native-source']['if']
    build=(ROOT/'.github/workflows/hook-windows-build.yml').read_text()
    assert "refs/heads/"+branch in build and 'codex/gray-release-0.10.x' not in build

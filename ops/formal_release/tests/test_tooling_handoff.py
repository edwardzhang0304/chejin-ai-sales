"""Negative provenance/coverage checks for partial release-tool evidence reuse."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ops/formal_release'))
import tooling_handoff as h


@pytest.fixture
def origin():
    report = {'schema_version': 2, 'kind': 'tooling', 'status': 'passed', 'commit': 'a'*40,
              'run_id': '42', 'attempt': '1', 'fingerprint': 'f'*64,
              'scope': [{'cwd': '.', 'nodes': ['ops/formal_release/tests/test_old.py']}],
              'results': [{'exit_code': 0, 'passed': 1, 'failed': 0, 'skipped': 0}]}
    run = {'id': 42, 'run_attempt': 1, 'head_sha': 'a'*40, 'head_branch': 'gray-release-0.10.x',
           'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure',
           'repository': {'full_name': h.REPO}, 'path': h.WORKFLOW}
    jobs = {'jobs': [{'id': 7, 'name': 'Release tools checks', 'run_attempt': 1, 'conclusion': 'success'}]}
    record = {'run_id': 42, 'attempt': 1, 'commit': 'a'*40, 'branch': 'gray-release-0.10.x',
              'job_id': 7, 'artifact_id': 9}
    artifacts = {'artifacts': [{'name': 'tooling-evidence-42-1', 'id': 9, 'expired': False, 'size_in_bytes': 100}]}
    def payload(xml='<testsuites><testsuite><testcase classname="ops.formal_release.tests.test_old" name="test_old"/></testsuite></testsuites>'):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('evidence.json',json.dumps(report));z.writestr('pytest-0.xml',xml)
        raw=stream.getvalue();record['artifact_sha256']=hashlib.sha256(raw).hexdigest();return raw
    raw=payload()
    def api(path, binary=False):
        return {'actions/runs/42/attempts/1':run,'actions/runs/42/attempts/1/jobs?per_page=100':jobs,
                'actions/runs/42/artifacts?per_page=100':artifacts,'actions/artifacts/9/zip':raw}[path]
    return record,run,jobs,artifacts,report,payload,api


def call_original(o):
    with patch.object(h,'api',side_effect=o[-1]), patch('source_evidence.fingerprint',return_value='f'*64), patch('source_evidence.git',return_value=b'original workflow'):
        return h.original(o[0])


def test_actual_original_attempt_pass_is_reusable_despite_unrelated_job_failure(origin):
    groups=call_original(origin)
    assert groups[0][1][0].get('name')=='test_old'
    assert origin[0]['attempt']==1


@pytest.mark.parametrize('target,key,value',[
    (1,'run_attempt',2),(1,'head_sha','b'*40),(1,'repository',{'full_name':'other/repo'}),
    (1,'head_branch','other'),(1,'path','wrong.yml'),(1,'status','in_progress'),
])
def test_original_identity_cannot_be_relabelled(origin,target,key,value):
    origin[target][key]=value
    with pytest.raises(ValueError):call_original(origin)


@pytest.mark.parametrize('change', ['failed_job','wrong_job_id','wrong_attempt','expired','wrong_artifact','hash'])
def test_invalid_original_component_rejected(origin,change):
    r,run,jobs,arts,*_=origin
    if change=='failed_job':jobs['jobs'][0]['conclusion']='failure'
    elif change=='wrong_job_id':jobs['jobs'][0]['id']=8
    elif change=='wrong_attempt':jobs['jobs'][0]['run_attempt']=2
    elif change=='expired':arts['artifacts'][0]['expired']=True
    elif change=='wrong_artifact':arts['artifacts'][0]['id']=10
    else:r['artifact_sha256']='0'*64
    with pytest.raises(ValueError):call_original(origin)


@pytest.mark.parametrize('marker', ['<failure/>','<skipped/>','<error/>'])
def test_failed_or_skipped_xml_never_reused(origin,marker):
    raw=origin[5](f'<testsuites><testsuite><testcase>{marker}</testcase></testsuite></testsuites>')
    oldapi=origin[-1]
    with patch.object(h,'api',side_effect=lambda path,**kw:raw if path.endswith('/zip') else oldapi(path)),patch('source_evidence.fingerprint',return_value='f'*64),patch('source_evidence.git',return_value=b'workflow'):
        with pytest.raises(ValueError):h.original(origin[0])


@pytest.fixture
def handoff(tmp_path):
    selected='ops/formal_release/tests/test_new.py';old='ops/formal_release/tests/test_old.py'
    filename='ops/formal_release/handoffs/tool-reuse.json';scopefile='ops/formal_release/handoffs/tool-scope.json'
    before={old:'100644 blob old','ops/formal_release/changed.py':'100644 blob old'}
    after={**before,'ops/formal_release/changed.py':'100644 blob new',selected:'100644 blob test',scopefile:'100644 blob scope'}
    scope={'original':{'commit':'a'*40},'changed_inputs':{n:{'reason':'reviewed dependency impact','covered_by':[selected]} for n in after if before.get(n)!=after[n]},
           'pytest':[{'cwd':'.','nodes':[selected]}],'unaffected_scope_review':'Old test exercises an unchanged independent module.'}
    data={'schema_version':1,'repository':h.REPO,'reviewed_commit':'b'*40,'scope_file':scopefile,'reviewed_inputs':after,
          'review':{'result':'passed','thread_id':'reviewer','turn_id':'turn','evidence_sha256':'f'*64}}
    p=tmp_path/filename;p.parent.mkdir(parents=True);p.write_text(json.dumps(data))
    cases=[ET.fromstring('<testcase classname="ops.formal_release.tests.test_old" name="test_old"/>')]
    group=({'cwd':'.','nodes':[old]},cases)
    def run():
        with patch.object(h,'ROOT',tmp_path),patch.object(h,'git',return_value=json.dumps(scope).encode()),patch.object(h,'inputs',side_effect=[before,after,after]),patch.object(h,'original',return_value=[group]):
            return h.validate(filename)
    return scope,data,p,before,after,group,run


def test_reuse_has_original_case_identity_and_separate_execution_selection(handoff):
    r=handoff[-1]();assert r['count']==1 and r['cases'][0]['mode']=='reused'
    assert r['pytest'][0]['nodes']==['ops/formal_release/tests/test_new.py']


@pytest.mark.parametrize('mutation', ['unreviewed','empty_plan','missing_impact','missing_coverage','changed_test','ambiguous_scope','missing_case'])
def test_unreviewed_or_incomplete_scope_is_rejected(handoff,mutation):
    s,d,p,b,a,g,run=handoff
    if mutation=='unreviewed':a['ops/formal_release/unreviewed.py']='100755 blob x'
    elif mutation=='empty_plan':s['pytest']=[]
    elif mutation=='missing_impact':s['changed_inputs'].pop('ops/formal_release/changed.py')
    elif mutation=='missing_coverage':s['changed_inputs']['ops/formal_release/changed.py']['covered_by']=['not-selected.py']
    elif mutation=='changed_test':a['ops/formal_release/tests/test_old.py']='changed'
    elif mutation=='ambiguous_scope':g[0]['nodes']*=2
    else:g[1].clear()
    with pytest.raises(ValueError):run()


def test_diagnostic_job_is_not_full_tooling_and_push_skip_remains_operator_condition():
    w=yaml.load((ROOT/'.github/workflows/release-evidence.yml').read_text(),Loader=yaml.BaseLoader)
    job=w['jobs']['windows-parser']
    assert 'Windows GUI driver diagnostic only' in job['name']
    for step in job['steps']:
        if step.get('name','').startswith(('Extract formal','Parse formal','Exercise Hook')):
            assert "inputs.check_kind != 'driver-smoke'" in step['if']
    assert 'driver-smoke' not in w['jobs']['tooling']['if']
    assert w['jobs']['tooling']['permissions']=={'contents':'read','actions':'read'}
    assert w['jobs']['tooling']['env']['GH_TOKEN']=='${{ github.token }}'
    assert 'TOOLING_HANDOFF' in next(s for s in w['jobs']['tooling']['steps'] if s.get('name')=='Test changed release tooling and record evidence')['env']

@pytest.mark.parametrize('key,value', [('attempt','2'),('kind','source'),('status','failed'),('scope',[])])
def test_wrong_original_report_rejected(origin,key,value):
    origin[4][key]=value
    raw=origin[5]()
    oldapi=origin[-1]
    with patch.object(h,'api',side_effect=lambda path,**kw:raw if path.endswith('/zip') else oldapi(path)),patch('source_evidence.fingerprint',return_value='f'*64),patch('source_evidence.git',return_value=b'workflow'):
        with pytest.raises(ValueError):h.original(origin[0])


def test_resume_without_handoff_fails_instead_of_running_all_tests(tmp_path,monkeypatch):
    import run_evidence_checks as runner
    monkeypatch.setenv('CHECK_KIND','tooling');monkeypatch.setenv('EVIDENCE_DIR',str(tmp_path))
    monkeypatch.setenv('TOOLING_REUSE_REQUIRED','true');monkeypatch.delenv('TOOLING_HANDOFF',raising=False)
    with patch.object(runner,'identity',return_value={}),patch.object(runner,'execute') as execute:
        with pytest.raises(ValueError,match='TOOLING_RESUME_HANDOFF_REQUIRED'):runner.main()
        execute.assert_not_called()


def test_resume_reports_reused_and_executed_separately_and_runs_only_selection(tmp_path,monkeypatch):
    import run_evidence_checks as runner
    monkeypatch.setenv('CHECK_KIND','tooling');monkeypatch.setenv('EVIDENCE_DIR',str(tmp_path))
    monkeypatch.setenv('TOOLING_REUSE_REQUIRED','true');monkeypatch.setenv('TOOLING_HANDOFF','reviewed.json')
    monkeypatch.setenv('CHEJIN_RELEASE_TRANSPORT','ocr')
    node='ops/formal_release/tests/test_tooling_handoff.py'
    reuse={'count':7,'cases':[{'mode':'reused'}], 'original':{'run_id':42,'attempt':1},'pytest':[{'cwd':'.','nodes':[node]}]}
    def execute(argv,cwd,path,environment):
        assert argv[3:-3]==[node]  # sys.executable -m pytest <only selected> -q --junitxml path
        Path(argv[-1]).write_text('<testsuites><testsuite><testcase name="new"/></testsuite></testsuites>')
        return 0
    with patch.object(runner,'identity',return_value={'commit':'b'*40,'run_id':'43','attempt':'1'}),patch.object(h,'validate',return_value=reuse),patch.object(runner,'fingerprint',return_value='f'*64),patch.object(runner,'execute',side_effect=execute):
        runner.main()
    report=json.loads((tmp_path/'evidence.json').read_text())
    assert report['results'][0]['type']=='original_ci_evidence_reuse'
    assert report['results'][0]['passed']==7 and report['results'][0]['tests_executed']==0
    assert report['results'][1]['passed']==1
    assert report['tooling_reuse']['original']=={'run_id':42,'attempt':1}

"""Reuse exact completed source suites, never an unverified package or failed suite."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_RUN = '34468407574'
ORIGINAL_COMMIT = '0937570ef474f0c10029b86fbf77fb2e750ff174'
REPO = 'edwardzhang0304/chejin-ai-sales'
SUITES = ('schema', 'credentials', 'unittest', 'ui_bridge', 'run_add_friend_package_smoke.py',
          'run_wechat_win32_ocr_compat_checks.py', 'run_wechat_win32_ocr_env_config_checks.py',
          'run_wechat_win32_ocr_interaction_evidence_checks.py', 'run_wechat_win32_ocr_humanized_input_checks.py',
          'run_wechat_startup_calibration_v0923_checks.py', 'smoke_e2e', 'compile')
# Only release orchestration and the failed (therefore never reused) fixture may change.
REPAIR_FILES = {
 '.github/workflows/worker-windows-package.yml',
 'ops/formal_release/source_check_reuse.py', 'ops/formal_release/tests/test_source_check_reuse.py',
 'worker-client/run_checks.py',
 'worker-client/omniauto-rpa/apps/wechat_ai_customer_service/tests/run_wechat_win32_ocr_compat_checks.py',
 'deliverables/AI智能客服售前跟进系统_PRD_运营后台统一版_v0.9.68.md',
 'deliverables/AI智能客服售前跟进系统_技术方案手册_v0.9.68.md',
 'deliverables/AI智能客服售前跟进系统_版本更新记录.md',
 'deliverables/AI智能客服售前跟进系统_全流程图_v0.9.68.puml',
}


def require(ok, code):
    if not ok: raise ValueError(code)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def fingerprint(ref, *, repair=False, repair_files=None):
    entries=[]
    for row in git('ls-tree','-rz',ref).split(b'\0'):
        if not row: continue
        _,name=row.split(b'\t',1)
        if repair and name.decode() in (REPAIR_FILES if repair_files is None else repair_files): continue
        entries.append(row.decode())
    return hashlib.sha256(json.dumps(entries,ensure_ascii=False).encode()).hexdigest()


def validate_origin(run, jobs, log):
    require(str(run['id'])==ORIGINAL_RUN and run['head_sha']==ORIGINAL_COMMIT
            and run['head_branch']=='codex/gray-release-0.9.x'
            and run['path']=='.github/workflows/worker-windows-package.yml'
            and run['event']=='workflow_dispatch' and run['status']=='completed', 'UNTRUSTED_SOURCE_RUN')
    job=next(j for j in jobs if j['name']=='Build signed formal Windows package')
    require(job['conclusion']=='failure', 'UNEXPECTED_ORIGINAL_JOB')
    for name in ('Fail fast on native Windows handoff checks','Run shared source checks'):
        require(any(s['name']==name and s['conclusion']=='success' for s in job['steps']), 'PRIOR_STAGE_NOT_PASSED')
    lines=[re.sub(r'^.*?\d{4}-\d\d-\d\dT\S+Z ?', '', line).strip() for line in log.splitlines()]
    text='\n'.join(lines)
    require(re.search(r'Ran 1108 tests in 929[.]505s\n\nOK(?:\n|$)',text), 'UNITTEST_PASS_EVIDENCE_MISSING')
    require('# tests 4' in lines and '# pass 4' in lines and '# fail 0' in lines, 'UI_PASS_EVIDENCE_MISSING')
    require('All 47 add_friend package smoke checks passed.' in lines, 'ADD_FRIEND_PASS_EVIDENCE_MISSING')
    require("AttributeError: '_CompatStructuralImage' object has no attribute 'height'" in lines, 'UNEXPECTED_FAILURE')
    return ['schema','credentials','unittest','ui_bridge','run_add_friend_package_smoke.py']


def save(path, receipt):
    Path(path).write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return receipt


def load_receipt(path):
    receipt=json.loads(Path(path).read_text(encoding='utf-8'))
    require(receipt['schema_version']==1 and receipt['current_commit']==git('rev-parse','HEAD').decode().strip(), 'RECEIPT_COMMIT_MISMATCH')
    require(receipt['current_tree_sha256']==fingerprint('HEAD'), 'RECEIPT_TREE_MISMATCH')
    require(receipt['run_id']==os.environ.get('GITHUB_RUN_ID'), 'RECEIPT_RUN_MISMATCH')
    require(receipt['run_attempt']==os.environ.get('GITHUB_RUN_ATTEMPT'), 'RECEIPT_ATTEMPT_MISMATCH')
    require(set(receipt['completed_suites'])<=set(SUITES), 'UNKNOWN_REUSED_SUITE')
    require(receipt.get('mode') in {'same_run_complete','verified_partial_source','verified_shared_prefix'}, 'UNVERIFIED_RECEIPT_MODE')
    if receipt['mode']=='same_run_complete':
        require(set(receipt['completed_suites'])==set(SUITES), 'INCOMPLETE_CURRENT_RUN')
    elif receipt['mode']=='verified_shared_prefix':
        require(receipt.get('original_run')==PREFIX_RUN and receipt.get('original_commit')==PREFIX_COMMIT
                and receipt.get('native_stage')=='passed' and receipt.get('native_long_path')=='passed'
                and set(receipt['completed_suites'])=={'credentials'}, 'INVALID_PREFIX_RECEIPT')
    else:
        require(receipt.get('original_run')==ORIGINAL_RUN and receipt.get('original_commit')==ORIGINAL_COMMIT
                and receipt.get('native_stage')=='passed' and receipt.get('shared_stage')=='passed'
                and set(receipt['completed_suites'])=={'schema','credentials','unittest','ui_bridge','run_add_friend_package_smoke.py'}, 'INVALID_PARTIAL_RECEIPT')
    return receipt


def complete(path, suites):
    require(set(suites)==set(SUITES),'SOURCE_CHECKS_INCOMPLETE')
    return save(path, {'schema_version':1,'current_commit':git('rev-parse','HEAD').decode().strip(),
      'current_tree_sha256':fingerprint('HEAD'),'run_id':os.environ['GITHUB_RUN_ID'],
      'run_attempt':os.environ['GITHUB_RUN_ATTEMPT'],'completed_suites':list(SUITES),'mode':'same_run_complete'})


def resolve(run_id, output):
    if run_id == PREFIX_RUN:
        return resolve_prefix(run_id, output)
    require(run_id==ORIGINAL_RUN, 'UNSUPPORTED_REUSE_RUN')
    require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REPOSITORY')==REPO,'TRUSTED_CI_REQUIRED')
    require(fingerprint(ORIGINAL_COMMIT,repair=True)==fingerprint('HEAD',repair=True),'REUSED_SOURCE_CHANGED')
    def api(path):return json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/'+path],text=True,encoding='utf-8'))
    run=api('actions/runs/'+run_id);jobs=api('actions/runs/'+run_id+'/jobs?per_page=100')['jobs']
    log=subprocess.check_output(['gh','run','view',run_id,'--repo',REPO,'--log-failed'],text=True,encoding='utf-8')
    suites=validate_origin(run,jobs,log)
    receipt={'schema_version':1,'current_commit':git('rev-parse','HEAD').decode().strip(),
      'current_tree_sha256':fingerprint('HEAD'),'run_id':os.environ['GITHUB_RUN_ID'],
      'run_attempt':os.environ['GITHUB_RUN_ATTEMPT'],'completed_suites':suites,'mode':'verified_partial_source',
      'original_run':run_id,'original_commit':ORIGINAL_COMMIT,'original_log_sha256':hashlib.sha256(log.encode()).hexdigest(),
      'native_stage':'passed','shared_stage':'passed','unittest_passed':1108,'ui_bridge_passed':4,'add_friend_passed':47}
    save(output,receipt)
    export_flags({'CHEJIN_SHARED_CHECKS_COMPLETE':'true'})
    print(json.dumps({'source_reuse':'verified','original_run':run_id,'completed_suites':suites}))



PREFIX_RUN = '34567516324'
PREFIX_COMMIT = 'ae23705c3317c7ebd2970879b9e57756c5f889c0'
PREFIX_REPAIRS = {
 'backend/app/services/release_readiness.py', 'backend/tests/test_release_readiness.py',
 'ops/formal_release/tests/test_candidate.py',
 '.github/workflows/worker-windows-package.yml', '.github/actions/worker-release-checks/action.yml',
 'ops/formal_release/source_check_reuse.py', 'ops/formal_release/tests/test_source_check_reuse.py',
 'ops/formal_release/install.sh', 'ops/formal_release/maintenance.py',
 'ops/formal_release/tests/test_release_environment.py', 'worker-client/tests/test_ui_contract.py',
 *[p for p in REPAIR_FILES if p.startswith('deliverables/')],
}


def export_flags(flags):
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as out:
        for key, value in flags.items(): out.write(key+'='+value+'\n')


def validate_prefix(run, jobs, log):
    require(str(run['id'])==PREFIX_RUN and run['head_sha']==PREFIX_COMMIT
            and run['head_branch']=='codex/gray-release-0.9.x' and run['event']=='workflow_dispatch'
            and run['path']=='.github/workflows/worker-windows-package.yml' and run['status']=='completed', 'UNTRUSTED_PREFIX_RUN')
    job=next(j for j in jobs if j['name']=='Build signed formal Windows package')
    require(job['conclusion']=='failure', 'UNEXPECTED_PREFIX_JOB')
    for name in ('Fail fast on native Windows handoff checks','Native Windows long-path negative and repaired controls'):
        require(any(s['name']==name and s['conclusion']=='success' for s in job['steps']), 'NATIVE_PREFIX_NOT_PASSED')
    for step_id in ('__self.__run','__self.__run_2'):
        require('end-action id='+step_id+';outcome=success;conclusion=success;' in log, 'SHARED_PREFIX_NOT_PASSED')
    require('test_v16_component_ui_assets_are_packaged' in log and 'FAILED (failures=1)' in log
            and 'end-action id=__self.__run_3;outcome=failure;conclusion=failure;' in log, 'UNEXPECTED_SHARED_PREFIX_FAILURE')
    return ['credentials']


def validate_prefix_commands(old_workflow, new_workflow, old_action, new_action):
    # Exact known condition additions only; every command/dependency byte stays protected.
    expected = old_workflow.replace(
        "      - name: Native Windows long-path negative and repaired controls\n",
        "      - name: Native Windows long-path negative and repaired controls\n        if: env.CHEJIN_LONG_PATH_REUSED != 'true'\n").replace(
        "      - name: Run shared source checks\n        if: env.CHEJIN_SOURCE_CHECK_RECEIPT == ''",
        "      - name: Run shared source checks\n        if: env.CHEJIN_SHARED_CHECKS_COMPLETE != 'true'")
    require(expected == new_workflow, 'REUSED_WINDOWS_COMMANDS_CHANGED')
    expected = old_action.replace(
        "    - name: Run credential security gate\n",
        "    - name: Run credential security gate\n      if: github.workflow != 'Worker Windows package gate' || env.CHEJIN_SHARED_CREDENTIALS_REUSED != 'true'\n").replace(
        "    - name: Run affected Worker and backend read-settlement tests\n",
        "    - name: Run affected Worker and backend read-settlement tests\n      if: github.workflow != 'Worker Windows package gate' || env.CHEJIN_SHARED_SETTLEMENT_REUSED != 'true'\n")
    require(expected == new_action, 'REUSED_SHARED_COMMANDS_CHANGED')


def resolve_prefix(run_id, output):
    require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REPOSITORY')==REPO,'TRUSTED_CI_REQUIRED')
    require(fingerprint(PREFIX_COMMIT,repair=True,repair_files=PREFIX_REPAIRS)==fingerprint('HEAD',repair=True,repair_files=PREFIX_REPAIRS), 'REUSED_PREFIX_SOURCE_CHANGED')
    validate_prefix_commands(
        git('show',PREFIX_COMMIT+':.github/workflows/worker-windows-package.yml').decode(),
        git('show','HEAD:.github/workflows/worker-windows-package.yml').decode(),
        git('show',PREFIX_COMMIT+':.github/actions/worker-release-checks/action.yml').decode(),
        git('show','HEAD:.github/actions/worker-release-checks/action.yml').decode())
    def api(path):return json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/'+path],text=True,encoding='utf-8'))
    run=api('actions/runs/'+run_id);jobs=api('actions/runs/'+run_id+'/jobs?per_page=100')['jobs']
    log=subprocess.check_output(['gh','run','view',run_id,'--repo',REPO,'--log-failed'],text=True,encoding='utf-8')
    suites=validate_prefix(run,jobs,log)
    receipt={'schema_version':1,'current_commit':git('rev-parse','HEAD').decode().strip(),
      'current_tree_sha256':fingerprint('HEAD'),'run_id':os.environ['GITHUB_RUN_ID'],'run_attempt':os.environ['GITHUB_RUN_ATTEMPT'],
      'mode':'verified_shared_prefix','completed_suites':suites,'original_run':run_id,'original_commit':PREFIX_COMMIT,
      'original_log_sha256':hashlib.sha256(log.encode()).hexdigest(),'native_stage':'passed','native_long_path':'passed'}
    save(output,receipt)
    export_flags({'CHEJIN_SHARED_CREDENTIALS_REUSED':'true','CHEJIN_SHARED_SETTLEMENT_REUSED':'true','CHEJIN_LONG_PATH_REUSED':'true'})
    print(json.dumps({'source_reuse':'verified_shared_prefix','original_run':run_id,'failed_and_unexecuted_checks':'must_run'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();resolve(args.run_id,args.output)

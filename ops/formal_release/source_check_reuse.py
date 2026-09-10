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


def fingerprint(ref, *, repair=False):
    entries=[]
    for row in git('ls-tree','-rz',ref).split(b'\0'):
        if not row: continue
        _,name=row.split(b'\t',1)
        if repair and name.decode() in REPAIR_FILES: continue
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
    require(receipt.get('mode') in {'same_run_complete','verified_partial_source'}, 'UNVERIFIED_RECEIPT_MODE')
    if receipt['mode']=='same_run_complete':
        require(set(receipt['completed_suites'])==set(SUITES), 'INCOMPLETE_CURRENT_RUN')
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
    print(json.dumps({'source_reuse':'verified','original_run':run_id,'completed_suites':suites}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();resolve(args.run_id,args.output)

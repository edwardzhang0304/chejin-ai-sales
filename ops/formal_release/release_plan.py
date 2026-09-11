"""Validated per-release data. No executable commands, credentials or release constants."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from verify import COMMIT, SHA, VERSION, require

ROOT = Path(__file__).resolve().parents[2]
MODES = {'build_only', 'build_and_stage', 'accept_candidate', 'retest_candidate', 'stage_existing', 'check_staged', 'publish_staged'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def version(value):
    require(isinstance(value, str) and VERSION.fullmatch(value), 'INVALID_VERSION')
    return tuple(map(int, value.split('.')))


def validate(plan):
    require(isinstance(plan, dict) and set(plan) == {'schema_version', 'version', 'source_commit', 'old_client', 'route', 'recovery', 'scope_reason', 'source_evidence_run_id', 'tool_evidence_run_id', 'case_evidence_runs'}, 'INVALID_RELEASE_PLAN_FIELDS')
    require(type(plan['schema_version']) is int and plan['schema_version'] == 1, 'INVALID_PLAN_SCHEMA')
    old = plan['old_client']
    require(isinstance(old, dict) and set(old) == {'version', 'source_commit', 'run_id', 'artifact_id', 'zip_sha256', 'exe_sha256', 'updater_sha256'}, 'INVALID_OLD_CLIENT_FIELDS')
    require(version(plan['version']) > version(old['version']), 'TARGET_MUST_BE_NEWER')
    for commit in (plan['source_commit'], old['source_commit']):
        require(isinstance(commit, str) and COMMIT.fullmatch(commit), 'INVALID_SOURCE_COMMIT')
    for key in ('zip_sha256', 'exe_sha256', 'updater_sha256'):
        require(isinstance(old[key], str) and SHA.fullmatch(old[key]), 'INVALID_OLD_PACKAGE_HASH')
    require(plan['route'] in {'manual', 'button'} and plan['recovery'] in {'none', 'pending_read'}, 'INVALID_RELEASE_ROUTE')
    # Pending-read fixture proves manual preservation, never a button recovery.
    require(not (plan['route'] == 'button' and plan['recovery'] != 'none'), 'BUTTON_PENDING_RECOVERY_NOT_IMPLEMENTED')
    require(isinstance(plan['scope_reason'], str) and 1 <= len(plan['scope_reason'].strip()) <= 1000, 'SCOPE_REASON_REQUIRED')
    require(isinstance(plan['case_evidence_runs'], list) and len(plan['case_evidence_runs']) <= 20, 'INVALID_REUSE_RUNS')
    for value in [old['run_id'], old['artifact_id'], plan['source_evidence_run_id'], plan['tool_evidence_run_id'], *plan['case_evidence_runs']]:
        require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value), 'INVALID_EVIDENCE_ID')
    require(len(set(plan['case_evidence_runs'])) == len(plan['case_evidence_runs']), 'DUPLICATE_REUSE_RUN')
    policy = read(ROOT / 'ops/formal_release/release-policy.json')
    require(version(old['version']) >= version(policy['minimum_supported_client']), 'UNSUPPORTED_UPGRADE_START')
    require(not (plan['route'] == 'button' and old['version'] in policy['retired_button_starts']), 'BUTTON_START_RETIRED')
    return plan


def cases(plan):
    validate(plan)
    result = [plan['route'] + '_' + state for state in ('paused', 'faulted')]
    if plan['recovery'] == 'pending_read':
        result.append('pending_read')
    return result


def load(path=None):
    return validate(read(path or os.environ['CHEJIN_RELEASE_PLAN']))


def check_source(plan, root=ROOT):
    contract = read(root / 'contracts/c2_contract_v3.json')
    revision = contract.get('contract_revision')
    init = (root / 'worker-client/chejin_worker_client/__init__.py').read_text(encoding='utf-8')
    match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)', init)
    require(match and match[1] == revision == plan['version'], 'APPLICATION_CONTRACT_PLAN_MISMATCH')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if os.environ.get('DELIVERY_MODE') in {'build_only', 'build_and_stage'}:
        require(head == plan['source_commit'], 'BUILD_SOURCE_NOT_EXACT')
    return head


def export(plan, path):
    old = plan['old_client']
    values = {'CHEJIN_RELEASE_PLAN': str(path), 'TARGET_VERSION': plan['version'], 'CURRENT_VERSION': old['version'],
              'SOURCE_RUN_ID': plan['source_evidence_run_id'], 'TOOL_RUN_ID': plan['tool_evidence_run_id'],
              'OLD_SOURCE_COMMIT': old['source_commit'], 'OLD_RUN_ID': old['run_id'], 'OLD_ARTIFACT_ID': old['artifact_id'],
              'RELEASE_ROUTE': plan['route'], 'RELEASE_RECOVERY': plan['recovery']}
    for target in (os.environ.get('GITHUB_ENV'), os.environ.get('GITHUB_OUTPUT')):
        if target:
            with open(target, 'a', encoding='utf-8') as f:
                for k, v in values.items():
                    require('\n' not in v and '\r' not in v, 'UNSAFE_OUTPUT')
                    f.write(f'{k}={v}\n')


def verify_old_artifact(plan):
    from source_evidence import api, REPO
    from select_artifact import select
    old = plan['old_client']; run = api('actions/runs/' + old['run_id'])
    require(run.get('repository', {}).get('full_name') == REPO and run.get('status') == 'completed', 'UNTRUSTED_BASELINE_RUN')
    jobs = api(f"actions/runs/{old['run_id']}/attempts/{run['run_attempt']}/jobs?per_page=100")['jobs']
    artifacts = api(f"actions/runs/{old['run_id']}/artifacts?per_page=100")['artifacts']
    artifact, _ = select(run, jobs, artifacts)
    require(str(artifact) == old['artifact_id'], 'OLD_ARTIFACT_NOT_FROM_ACCEPTED_RUN')


def prevent_duplicate_build(plan):
    from source_evidence import api, BRANCH
    from candidate import select_candidate
    if os.environ.get('DELIVERY_MODE') not in {'build_only', 'build_and_stage'}:
        return
    runs = api('actions/workflows/worker-windows-package.yml/runs?branch=' + BRANCH + '&head_sha=' + plan['source_commit'] + '&per_page=100')['workflow_runs']
    for run in runs:
        if str(run['id']) == os.environ.get('GITHUB_RUN_ID'):
            continue
        jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")['jobs']
        artifacts = api(f"actions/runs/{run['id']}/artifacts?per_page=100")['artifacts']
        try:
            select_candidate(run, jobs, artifacts)
        except ValueError:
            continue
        raise ValueError('CANDIDATE_ALREADY_SAVED_USE_ACCEPT_CANDIDATE_RUN_' + str(run['id']))


def main():
    p = argparse.ArgumentParser(); p.add_argument('--file', type=Path); p.add_argument('--export', action='store_true'); p.add_argument('--preflight', action='store_true')
    args = p.parse_args()
    plan = validate(read(args.file) if args.file else json.loads(os.environ['RELEASE_PLAN_JSON']))
    check_source(plan)
    if args.preflight:
        verify_old_artifact(plan); prevent_duplicate_build(plan)
    path = Path(os.environ.get('RUNNER_TEMP', '.')) / 'formal-release-plan.json'
    if args.export:
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8'); export(plan, path)
    print(json.dumps({'version': plan['version'], 'current_version': plan['old_client']['version'], 'route': plan['route'], 'cases': cases(plan), 'plan_valid': True}))

if __name__ == '__main__': main()

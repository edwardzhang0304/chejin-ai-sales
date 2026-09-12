"""Run only missing Windows cases; reuse successful reports from authenticated CI runs."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile
from candidate import read, source_inputs, verify_candidate, accept, accept_manual
from release_plan import load, cases
from source_evidence import api, git, REPO, BRANCH, FORMAL
from verify import digest, require

ROOT = Path(__file__).resolve().parents[2]
GUI = 'worker-client/scripts/run-windows-client-upgrade-test.py'
PENDING = 'worker-client/scripts/run-windows-pending-read-install.py'
BASE_TOOLS = ['ops/formal_release/acceptance_cases.py', 'ops/formal_release/release_plan.py',
              'ops/formal_release/candidate.py', 'ops/formal_release/verify.py', GUI,
              'worker-client/requirements.txt', 'worker-client/requirements-test.txt', 'backend/requirements.txt']


def sha_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def environment():
    return {'system': platform.system(), 'image_os': os.environ.get('ImageOS'),
            'image_version': os.environ.get('ImageVersion'), 'python': platform.python_version()}


def case_identity(plan, proof, name, commit, env):
    require(name in cases(plan), 'UNREQUESTED_CASE')
    files = BASE_TOOLS + ([PENDING, 'backend/tests/test_c2_historical_ocr_settlement.py'] if name == 'pending_read' else [])
    tools = {p: hashlib.sha256(git('show', f'{commit}:{p}')).hexdigest() for p in files}
    return sha_json({'case': name, 'target_version': proof['version'], 'build_commit': proof['build_commit'],
                     'archive': proof['files'][f"chejin-worker-v{proof['version']}-windows-x64.zip"],
                     'runtime': source_inputs(commit), 'baseline': plan['old_client'], 'tools': tools,
                     'environment': env})


def validate_case(name, report, plan, proof):
    old = plan['old_client']; stem = f"chejin-worker-v{proof['version']}-windows-x64"
    require(report.get('status') == 'passed' and report.get('current_version') == old['version']
            and report.get('target_version') == proof['version'] and report.get('target_commit') == proof['build_commit']
            and report.get('target_zip_sha256') == proof['files'][stem + '.zip'], 'CASE_PACKAGE_MISMATCH')
    pending = name == 'pending_read'
    require(report.get('original_exe_sha256' if pending else 'old_exe_sha256') == old['exe_sha256']
            and report.get('original_updater_sha256' if pending else 'old_updater_sha256') == old['updater_sha256'], 'CASE_BASELINE_MISMATCH')
    if pending:
        require(report.get('mode') == 'pending_read_preserve_data_install', 'CASE_ROUTE_MISMATCH')
        flags = ['normal_close_used','original_pending_flow_preserved_at_install','original_data_directory_reused',
                 'original_outbox_bytes_preserved','original_flow_completed','stopped_after_recovery','target_ui_confirmed','real_exe_recovery']
    else:
        route, state = name.split('_', 1)
        require(report.get('initial_run_status') == state, 'CASE_STATE_MISMATCH')
        flags = ['original_worker_exited','protected_data_preserved','target_ui_confirmed','paused_intent_and_idle_gate_preserved']
        if route == 'manual':
            require(report.get('mode') == 'preserve_data_manual_install' and report.get('real_settings_button_clicked') is False
                    and report.get('original_updater_used') is False, 'MANUAL_NOT_BUTTON')
            flags += ['normal_close_used','target_program_manifest_verified','original_data_directory_reused']
        else:
            flags += ['real_settings_button_clicked','original_updater_used','actual_backend_download','runtime_threads_alive','immutable_handoff_baseline']
    require(all(report.get(k) is True for k in flags), 'CASE_PROTECTION_INCOMPLETE')


def trusted_bundle(run, jobs, artifact, bundle):
    require(run.get('path') == FORMAL and run.get('event') == 'workflow_dispatch'
            and run.get('head_branch') == BRANCH and run.get('repository', {}).get('full_name') == REPO
            and run.get('status') == 'completed', 'UNTRUSTED_CASE_RUN')
    require(any(j.get('name') == 'Accept exact Windows candidate' and j.get('run_attempt') == run['run_attempt']
                and j.get('status') == 'completed' for j in jobs), 'CASE_JOB_NOT_FINISHED')
    require(artifact['name'] == f"release-cases-{run['id']}-{run['run_attempt']}" and not artifact['expired'], 'CASE_ARTIFACT_MISMATCH')
    require(bundle.get('schema_version') == 1 and bundle.get('producer') == {
        'commit': run['head_sha'], 'run_id': str(run['id']), 'attempt': str(run['run_attempt'])}, 'CASE_PRODUCER_MISMATCH')
    require(bundle.get('environment', {}).get('system') == 'Windows', 'CASE_NOT_WINDOWS')
    require(isinstance(bundle.get('cases'), dict), 'INVALID_CASE_INDEX')
    return bundle


def fetch_runs(run_ids):
    bundles = []
    for run_id in run_ids:
        run = api(f'actions/runs/{run_id}')
        attempt = run['run_attempt']
        jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')['jobs']
        artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
        matching = [a for a in artifacts if a['name'] == f'release-cases-{run_id}-{attempt}' and not a['expired']]
        require(len(matching) == 1 and matching[0]['size_in_bytes'] <= 2_000_000, 'CASE_INDEX_MISSING_OR_TOO_LARGE')
        artifact = matching[0]
        with zipfile.ZipFile(io.BytesIO(api(f"actions/artifacts/{artifact['id']}/zip", binary=True))) as z:
            members = [i for i in z.infolist() if i.filename == 'cases.json']
            require(len(members) == 1 and members[0].file_size <= 2_000_000, 'INVALID_CASE_INDEX_ARCHIVE')
            bundle = json.loads(z.read(members[0]))
        bundles.append(trusted_bundle(run, jobs, artifact, bundle))
    return bundles


def reusable(bundles, name, identity, plan, proof):
    for bundle in bundles:
        entry = bundle['cases'].get(name, {})
        if entry.get('status') != 'passed' or entry.get('identity') != identity:
            continue
        report = entry.get('report')
        require(isinstance(report, dict) and entry.get('report_sha256') == sha_json(report), 'CASE_REPORT_TAMPERED')
        validate_case(name, report, plan, proof)
        return report, bundle['producer']
    return None


def extract(archive, target):
    target.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        require(sum(i.file_size for i in z.infolist()) <= 2 * 1024**3, 'EXPANDED_PACKAGE_TOO_LARGE')
        seen = set()
        for i in z.infolist():
            p = Path(i.filename.replace('\\', '/'))
            require(not p.is_absolute() and '..' not in p.parts and ':' not in str(p)
                    and p.parts and p.parts[0] == 'CheJinWorkerClient'
                    and (i.external_attr >> 16) & 0o170000 != 0o120000, 'UNSAFE_PACKAGE_PATH')
            normalized = str(p).casefold()
            require(normalized not in seen, 'DUPLICATE_PACKAGE_PATH')
            seen.add(normalized)
            destination = target / p
            if i.filename.endswith(('/', '\\')):
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as source, destination.open('xb') as output:
                    import shutil
                    shutil.copyfileobj(source, output)
    return target / 'CheJinWorkerClient'


def check_baseline(plan, folder):
    old = plan['old_client']; stem = f"chejin-worker-v{old['version']}-windows-x64"
    archive = folder / (stem + '.zip')
    require(digest(archive) == old['zip_sha256'], 'OLD_ZIP_IDENTITY_MISMATCH')
    return archive


def execute_case(name, plan_path, folder, old_root, target, old_source, work):
    plan = load(plan_path); stem = f"chejin-worker-v{plan['version']}-windows-x64"
    case_root = work / name
    # GUI seeding imports the Worker package; recovery also needs the old backend repository.
    source_root = old_source if name == 'pending_read' else old_source / 'worker-client'
    require((source_root / ('backend' if name == 'pending_read' else 'chejin_worker_client')).is_dir(), 'OLD_SOURCE_LAYOUT_MISMATCH')
    command = [sys.executable, str(ROOT / (PENDING if name == 'pending_read' else GUI)), '--plan', str(plan_path),
               '--old-package-root', str(old_root), '--old-source-root', str(source_root),
               '--target-package-root', str(target), '--archive', str(folder / (stem + '.zip')),
               '--release', str(folder / (stem + '.release.json')), '--work-root', str(case_root)]
    if name != 'pending_read':
        route, state = name.split('_', 1); command += ['--case', state]
        if route == 'manual': command.append('--manual-install')
    log = work / (name + '.log')
    with log.open('w', encoding='utf-8') as f:
        result = subprocess.run(command, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}, timeout=600)
    report_path = case_root / ('result.json' if name == 'pending_read' else name.split('_', 1)[1] + '/result.json')
    report = read(report_path) if report_path.is_file() else {'status': 'failed', 'failure': 'REPORT_MISSING'}
    require(result.returncode == 0, 'WINDOWS_CASE_FAILED_' + name.upper())
    return report


def run_cases(names, identities, bundles, plan, proof, execute, save):
    """One successful case is persisted before the next can fail."""
    entries = {}
    for name in names:
        prior = reusable(bundles, name, identities[name], plan, proof)
        try:
            report = prior[0] if prior else execute(name)
            validate_case(name, report, plan, proof)
            entries[name] = {'status': 'passed', 'identity': identities[name], 'report': report,
                             'report_sha256': sha_json(report), 'reused_from': prior[1] if prior else None}
            save(entries)
            print(json.dumps({'case': name, 'action': 'reused' if prior else 'executed', 'status': 'passed'}), flush=True)
        except Exception as exc:
            entries[name] = {'status': 'failed', 'identity': identities[name], 'error_type': type(exc).__name__}
            save(entries)
            raise
    return entries


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--folder', type=Path, required=True); parser.add_argument('--old-folder', type=Path, required=True)
    parser.add_argument('--old-source', type=Path, required=True); parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    require(os.name == 'nt', 'WINDOWS_REQUIRED')
    plan = load(args.plan); proof = read(args.folder / 'candidate.json')
    commit = git('rev-parse', 'HEAD').decode().strip()
    require(os.environ.get('GITHUB_REPOSITORY') == REPO and os.environ.get('GITHUB_ACTIONS') == 'true', 'TRUSTED_CI_REQUIRED')
    require(proof['version'] == plan['version'] and proof['build_commit'] == plan['source_commit'], 'PLAN_CANDIDATE_MISMATCH')
    verify_candidate(args.folder, proof['build_commit'], proof['build_run_id'], commit)
    args.work.mkdir(parents=True, exist_ok=False)
    producer = {'commit': commit, 'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': os.environ['GITHUB_RUN_ATTEMPT']}
    env = environment(); names = cases(plan)
    identities = {name: case_identity(plan, proof, name, commit, env) for name in names}
    bundles = fetch_runs(plan['case_evidence_runs'])
    old_archive = check_baseline(plan, args.old_folder)
    old = extract(old_archive, args.work / 'old')
    require(digest(old/'CheJinWorkerClient.exe') == plan['old_client']['exe_sha256']
            and digest(old/'CheJinUpdater.exe') == plan['old_client']['updater_sha256'], 'OLD_EXE_IDENTITY_MISMATCH')
    target = extract(args.folder / f"chejin-worker-v{plan['version']}-windows-x64.zip", args.work/'target')
    def save(entries):
        (args.work/'cases.json').write_text(json.dumps({'schema_version': 1, 'producer': producer, 'environment': env,
                                                      'cases': entries}, ensure_ascii=False, indent=2), encoding='utf-8')
    save({})
    entries = run_cases(names, identities, bundles, plan, proof,
        lambda name: execute_case(name, args.plan, args.folder, old, target, args.old_source, args.work), save)
    combined = args.work/'upgrade-result.json'
    combined.write_text(json.dumps({'status': 'passed', 'cases': [entries[n]['report'] for n in names if n != 'pending_read']}, ensure_ascii=False), encoding='utf-8')
    if plan['route'] == 'button':
        accept(args.folder, combined, commit, producer['run_id'], plan['old_client']['version'])
    else:
        pending = args.work/'pending-read-result.json'
        if 'pending_read' in entries: pending.write_text(json.dumps(entries['pending_read']['report']), encoding='utf-8')
        accept_manual(args.folder, combined, pending if pending.exists() else None, commit, producer['run_id'],
                      plan['old_client']['version'], recovery_required=plan['recovery'] == 'pending_read')

if __name__ == '__main__': main()

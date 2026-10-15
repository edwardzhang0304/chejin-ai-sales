"""Build only a standalone updater repair and reuse two immutable Worker packages."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from sign_existing_hook import api, require, validate_build

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.file_digest(Path(path).open('rb'), 'sha256').hexdigest()


def fetch_package(run_id, folder):
    run = api(f'actions/runs/{run_id}')
    artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    names = [a['name'].removeprefix('CheJinWorkerClient_V') for a in artifacts
             if a['name'].startswith('CheJinWorkerClient_V') and not a['expired']]
    require(len(names) == 1, 'ONE_SEALED_WORKER_REQUIRED')
    version = names[0]
    artifact = validate_build(run, artifacts, version)
    raw = api(f"actions/artifacts/{artifact['id']}/zip", binary=True)
    require(artifact['digest'] == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'ARTIFACT_HASH_MISMATCH')
    folder.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as outer:
        package = outer.read(f'CheJinWorkerClient_V{version}.zip')
    archive = folder / 'artifact.zip'
    archive.write_bytes(package)
    with zipfile.ZipFile(io.BytesIO(package)) as z:
        for info in z.infolist():
            target = (folder / info.filename).resolve()
            require(target.is_relative_to(folder.resolve()) and '\\' not in info.filename, 'UNSAFE_ARCHIVE_PATH')
        z.extractall(folder)
    installed = folder / 'CheJinWorkerClient'
    manifest = json.loads((installed / 'update-package-manifest.json').read_text(encoding='utf-8-sig'))
    require(manifest['version'] == version and manifest['git_commit'] == run['head_sha'], 'SEALED_IDENTITY_MISMATCH')
    require(manifest.get('transport') == 'hook', 'HOOK_ONLY')
    for rel, expected in manifest['files'].items():
        require(digest(installed / rel) == expected, 'SEALED_PAYLOAD_CHANGED')
    return {'version': version, 'source_commit': run['head_sha'], 'run_id': str(run_id),
            'artifact_id': str(artifact['id']), 'zip_sha256': digest(archive),
            'exe_sha256': digest(installed / 'CheJinWorkerClient.exe'),
            'updater_sha256': digest(installed / 'CheJinUpdater.exe')}


def prepare(args):
    area = args.root
    area.mkdir(parents=True)
    old = fetch_package(args.old_run, area / 'old')
    target = fetch_package(args.target_run, area / 'target')
    require(target['version'] == args.version and target['zip_sha256'] == args.sha256, 'WRONG_TARGET_PACKAGE')
    keys = (area / 'old/CheJinWorkerClient/_internal/release-signing-public-keys.json').read_bytes()
    require(keys == (area / 'target/CheJinWorkerClient/_internal/release-signing-public-keys.json').read_bytes(), 'TRUST_CHANGED')
    (area / 'public-keys.json').write_bytes(keys)
    artifacts = api(f'actions/runs/{args.publication_run}/artifacts?per_page=100')['artifacts']
    signed = [a for a in artifacts if a['name'] == 'hook-publication-' + args.version and not a['expired']]
    require(len(signed) == 1, 'SIGNED_DESCRIPTOR_REQUIRED')
    raw = api(f"actions/artifacts/{signed[0]['id']}/zip", binary=True)
    require(signed[0]['digest'] == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'DESCRIPTOR_ARTIFACT_CHANGED')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        descriptor = z.read('release.json')
    release = json.loads(descriptor)
    require(release['artifact_sha256'] == target['zip_sha256'] and release['git_commit'] == target['source_commit'], 'WRONG_SIGNED_TARGET')
    (area / 'release.json').write_bytes(descriptor)
    evidence = [a for a in api(f'actions/runs/{args.target_run}/artifacts?per_page=100')['artifacts']
                if a['name'] == 'native-build-evidence' and not a['expired']]
    require(len(evidence) == 1, 'SEALED_BUILD_EVIDENCE_REQUIRED')
    evidence_raw = api(f"actions/artifacts/{evidence[0]['id']}/zip", binary=True)
    require(evidence[0]['digest'] == 'sha256:' + hashlib.sha256(evidence_raw).hexdigest(), 'BUILD_EVIDENCE_CHANGED')
    with zipfile.ZipFile(io.BytesIO(evidence_raw)) as z:
        receipt = json.loads(z.read('native-source-evidence.json'))
    plan = {'schema_version': 1, 'version': args.version, 'source_commit': target['source_commit'],
            'old_client': old, 'route': 'button', 'recovery': 'none',
            'scope_reason': 'Updater-only packaging repair; original Worker and sealed target unchanged; isolated unbound GUI case.',
            'source_evidence_run_id': str(receipt['source']['run_id']),
            'tool_evidence_run_id': str(receipt['tooling']['run_id']), 'case_evidence_runs': []}
    (area / 'plan.json').write_text(json.dumps(plan, indent=2))
    subprocess.run(['git', 'worktree', 'add', '--detach', str(area / 'old-source'), old['source_commit']], cwd=ROOT, check=True)
    if args.repair_binary_run:
        run = api(f'actions/runs/{args.repair_binary_run}')
        require(run['head_branch'] == 'gray-release-0.10.x' and run['path'] == '.github/workflows/release-evidence.yml', 'UNTRUSTED_UPDATER_RUN')
        jobs = api(f'actions/runs/{args.repair_binary_run}/jobs?per_page=100')['jobs']
        require(any(j['name'] == 'Build only updater repair and exercise original Windows button'
                    and any(s['name'] == 'Build standalone updater only' and s['conclusion'] == 'success'
                            for s in j['steps']) for j in jobs), 'UPDATER_BUILD_NOT_PASSED')
        subprocess.run(['git', 'diff', '--exit-code', run['head_sha'], 'HEAD', '--',
            'worker-client/chejin_worker_client', 'worker-client/omniauto-rpa', 'contracts',
            'worker-client/packaging/chejin-updater.spec', 'worker-client/packaging/chejin_updater_entry.py',
            'worker-client/packaging/updater_runtime_hook.py'], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        artifacts = api(f'actions/runs/{args.repair_binary_run}/artifacts?per_page=100')['artifacts']
        candidates = [a for a in artifacts if a['name'] == 'frozen-updater-' + args.repair_binary_run and not a['expired']]
        require(len(candidates) == 1, 'SAVED_UPDATER_REQUIRED')
        raw = api(f"actions/artifacts/{candidates[0]['id']}/zip", binary=True)
        require(candidates[0]['digest'] == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'SAVED_UPDATER_CHANGED')
        dest = ROOT / 'worker-client/dist/CheJinUpdater.exe'
        dest.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            require(z.namelist() == ['CheJinUpdater.exe'], 'UNEXPECTED_UPDATER_ARTIFACT')
            dest.write_bytes(z.read('CheJinUpdater.exe'))
        (area / 'reused-updater.json').write_text(json.dumps({'run': args.repair_binary_run,
            'source_commit': run['head_sha'], 'sha256': digest(dest)}))
    with open(os.environ['GITHUB_ENV'], 'a') as env:
        env.write(f'CHEJIN_RELEASE_SIGNING_KEYS_PATH={area / "public-keys.json"}\n')


def assemble(args):
    area = args.root
    package = area / 'repair'
    package.mkdir()
    exe = ROOT / 'worker-client/dist/CheJinUpdater.exe'
    shutil.copyfile(exe, package / exe.name)
    baselines = []
    for mode in ('old', 'target'):
        root = area / mode / 'CheJinWorkerClient'
        manifest = json.loads((root / 'update-package-manifest.json').read_text(encoding='utf-8-sig'))
        baselines.append({'version': manifest['version'], 'worker_sha256': digest(root / 'CheJinWorkerClient.exe'),
                          'updater_sha256': digest(root / 'CheJinUpdater.exe'),
                          'manifest_sha256': digest(root / 'update-package-manifest.json')})
    repair = {'schema_version': 1, 'operation': 'updater_only_repair', 'updater_sha256': digest(exe),
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'baselines': baselines, 'worker_rebuilt': False, 'target_package_rebuilt': False}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64'], validate=True))
    canonical = json.dumps(repair, sort_keys=True, separators=(',', ':')).encode()
    repair['signature_key_id'] = os.environ['CHEJIN_RELEASE_SIGNING_KEY_ID']
    repair['signature'] = base64.b64encode(key.sign(canonical)).decode()
    trusted = json.loads((area / 'public-keys.json').read_text())
    public = next(k for k in trusted['keys'] if k['key_id'] == repair['signature_key_id'])
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public['public_key_base64'])).verify(
        base64.b64decode(repair['signature']), canonical)
    descriptor = package / 'repair.json'
    descriptor.write_text(json.dumps(repair, indent=2), encoding='utf-8')
    text = (ROOT / 'worker-client/packaging/repair-updater.ps1').read_text(encoding='utf-8-sig')
    (package / 'repair-updater.ps1').write_text(text.replace('REPAIR_DESCRIPTOR_SHA256_PLACEHOLDER', digest(descriptor)), encoding='utf-8-sig')
    (package / 'Repair-CheJinUpdater.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair-updater.ps1"\r\npause\r\n')
    # Execute the frozen EXE itself before testing any GUI handoff.
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('TOKEN', 'SECRET', 'SIGNING_PRIVATE'))}
    env.pop('CHEJIN_RELEASE_SIGNING_KEYS_PATH', None)
    env.pop('PSModulePath', None)
    env['CHEJIN_WORKER_HOME'] = str(area / 'smoke-data')
    result = subprocess.run([str(exe), '--help'], env=env, timeout=45, capture_output=True)
    require(result.returncode == 0, 'FROZEN_UPDATER_IMPORT_FAILED')
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    pyz = archive.open_embedded_archive(next(n for n in archive.toc if n.endswith('.pyz')))
    require('apps.wechat_ai_customer_service.adapters.client_release_rules' in pyz.toc, 'FROZEN_RULES_NOT_BUNDLED')
    # Exercise actual PowerShell repair, idempotency and restoration on both
    # untouched Worker packages. No test changes an application database.
    for mode in ('old', 'target'):
        original = area / mode / 'CheJinWorkerClient'
        probe = area / ('repair-probe-' + mode)
        probe.mkdir()
        for name in ('CheJinWorkerClient.exe', 'CheJinUpdater.exe', 'update-package-manifest.json'):
            shutil.copyfile(original / name, probe / name)
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(package / 'repair-updater.ps1'), '-InstallDirectory', str(probe)]
        subprocess.run(command, check=True, env=env); subprocess.run(command, check=True, env=env)
        require(digest(probe / exe.name) == digest(exe), 'REPAIR_COPY_FAILED')
        subprocess.run(command + ['-Restore'], check=True, env=env)
        require(all(digest(probe / n) == digest(original / n) for n in ('CheJinWorkerClient.exe', 'CheJinUpdater.exe', 'update-package-manifest.json')), 'REPAIR_RESTORE_FAILED')
    shutil.make_archive(str(area / ('CheJinUpdaterRepair_V' + args.version)), 'zip', package)
    (area / 'repair-checks.json').write_text(json.dumps({'frozen_updater_help': 'passed', 'shared_rules_bundled': True,
        'repair_idempotence_restore_both_versions': 'passed', 'updater_sha256': digest(exe), 'worker_rebuilt': False}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('operation', choices=['prepare', 'assemble'])
    for name in ('root', 'old-run', 'target-run', 'publication-run', 'version', 'sha256'):
        p.add_argument('--' + name, required=name in ('root', 'version'), type=Path if name == 'root' else str)
    p.add_argument('--repair-binary-run', default='')
    args = p.parse_args()
    (prepare if args.operation == 'prepare' else assemble)(args)

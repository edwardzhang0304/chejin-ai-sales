"""Publish an already verified Hook ZIP using root-owned operator approval.

No builds, migrations, application restarts, frontend switch or data cleanup.
Gray ingress remains separately configured; this command never widens it.
"""
import argparse
import json
from pathlib import Path

from receiver import require_publication_approval, run_fixed
from verify import digest, require
from verify_hook import verify_hook, trusted_path


def publish(folder, config, check_only=False):
    folder = trusted_path(folder)
    for name in ('release.json', 'artifact.zip', 'public-keys.json'):
        trusted_path(folder / name)
    approvals = trusted_path(config['component_approvals'])
    require(approvals != folder and folder not in approvals.parents and approvals not in folder.parents,
            'APPROVAL_MUST_BE_SEPARATE')
    approval_path = trusted_path(approvals / (digest(folder / 'artifact.zip') + '.json'))
    approval = json.loads(approval_path.read_text())
    descriptor = json.loads((folder / 'release.json').read_text())
    meta = {'version': descriptor['version'], 'commit': descriptor['git_commit'],
            'sha256': descriptor['artifact_sha256'], 'current_version': approval['old_client']['version']}
    require(descriptor['channel'] == 'gray-hook', 'HOOK_ONLY')
    require(digest(folder / 'artifact.zip') == meta['sha256'], 'ARTIFACT_HASH_MISMATCH')
    require((folder / 'public-keys.json').read_bytes() == Path(config['public_keys']).read_bytes(), 'SIGNING_TRUST_CHANGED')
    require_publication_approval(folder, meta, approval=approval)
    verified = verify_hook(folder, descriptor, approval, config)
    container = config['container']
    require(run_fixed(['docker', 'inspect', container, '--format', '{{.State.Health.Status}}'], text=True).strip() == 'healthy',
            'BACKEND_UNHEALTHY')
    target = '/tmp/chejin-formal-' + meta['sha256']
    run_fixed(['docker', 'exec', container, 'mkdir', '-p', target])
    try:
        for name in ('release.json', 'artifact.zip', 'public-keys.json'):
            run_fixed(['docker', 'cp', str(folder / name), container + ':' + target + '/' + name])
        # This is installed operator code. Never execute a script from the ZIP.
        result = json.loads(run_fixed(['docker', 'exec', '-i', container, 'python', '-', target,
                                      verified['contract_revision'], verified['contract_sha256'],
                                      'check' if check_only else 'publish'],
                                     input=Path(__file__).with_name('register.py').read_text(), text=True))
        require(result.get('ok') is True, 'REGISTRATION_FAILED')
        return {**meta, **result, 'backend_deployed': False, 'admin_deployed': False,
                'gray_ingress_changed': False, 'physical_acceptance': 'not_run'}
    finally:
        run_fixed(['docker', 'exec', container, 'rm', '-f',
                   *[target + '/' + n for n in ('release.json', 'artifact.zip', 'public-keys.json')]])
        run_fixed(['docker', 'exec', container, 'rmdir', target])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('/etc/chejin-formal-release.json'))
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(publish(args.folder, json.loads(trusted_path(args.config).read_text()), args.check_only)))


if __name__ == '__main__':
    main()

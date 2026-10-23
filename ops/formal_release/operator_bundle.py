"""One installation inventory for new and existing operator tools; no application deploy."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

MODULES = ('capacity.py', 'receiver.py', 'verify.py', 'register.py', 'maintenance.py',
           'build_admin.py', 'manual_publication.py', 'manual_readiness.py',
           'component_release.py', 'upgrade_evidence.py', 'publish_hook.py', 'verify_hook.py', 'operator_bundle.py')
# These are transported as fixed source into the existing API container, never
# imported by the host. The host has neither SQLAlchemy nor the app package.
CONTAINER_PAYLOADS = ('register.py', 'manual_readiness.py')
HOST_MODULES = tuple(n for n in MODULES if n not in CONTAINER_PAYLOADS)


def check_bundle(bundle):
    bundle = Path(bundle)
    for name in MODULES:
        file = bundle / name
        if not file.is_file() or file.is_symlink(): raise ValueError('OPERATOR_BUNDLE_INCOMPLETE:' + name)
    # Use ONLY the files that will actually be installed. No repository imports.
    with tempfile.TemporaryDirectory() as temp:
        for name in MODULES: shutil.copyfile(bundle / name, Path(temp) / name)
        script = ('import sys,ast;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                  + ';'.join('ast.parse((Path(sys.argv[1])/' + repr(n) + ').read_text())' for n in CONTAINER_PAYLOADS)
                  + ';' + ';'.join('import ' + n[:-3] for n in HOST_MODULES))
        # -I alone still runs site and editable-package import hooks. Host tools
        # need only stdlib at import time; prohibit site packages entirely.
        subprocess.run([sys.executable, '-I', '-S', '-c', script, temp], check=True, capture_output=True)


def merged_config(config, identities):
    result = dict(config)
    pinned = dict(config.get('client_baseline_identities', {}))
    if not isinstance(identities, dict): raise ValueError('BASELINE_IDENTITIES_INVALID')
    for version, identity in identities.items():
        if version not in config['client_baselines'] or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('BASELINE_NOT_INSTALLED')
        if not isinstance(identity, dict) or identity.get('version') != version or any(
            not re.fullmatch(r'[0-9a-f]{64}', str(identity.get(k, '')))
            for k in ('zip_sha256', 'exe_sha256', 'updater_sha256')):
            raise ValueError('BASELINE_IDENTITY_INVALID')
        if version in pinned and pinned[version] != identity: raise ValueError('BASELINE_IDENTITY_IMMUTABLE')
        pinned[version] = dict(identity)
    result['client_baseline_identities'] = pinned
    result.setdefault('component_approvals', '/var/lib/chejin-component-approvals')
    approvals = Path(result['component_approvals'])
    stage = Path(config['staging_root'])
    if not approvals.is_absolute() or approvals == stage or stage in approvals.parents or approvals in stage.parents:
        raise ValueError('APPROVAL_MUST_BE_SEPARATE')
    return result


def install_modules(bundle, target):
    check_bundle(bundle)
    target = Path(target); target.mkdir(parents=True, exist_ok=True)
    # Preflight all imports before replacing any installed entry. Existing keys,
    # client baselines, runtime environment and application directories untouched.
    for name in MODULES:
        dest = target / name
        if dest.is_symlink(): raise ValueError('UNSAFE_INSTALLED_MODULE')
    for name in MODULES:
        fd, temporary = tempfile.mkstemp(prefix='.module-', dir=target)
        with os.fdopen(fd, 'wb') as out: out.write((Path(bundle) / name).read_bytes())
        os.chmod(temporary, 0o644); os.replace(temporary, target / name)


def upgrade_tools(bundle, target, config_path, identities_path=None):
    from verify_hook import trusted_path
    config_path = trusted_path(config_path)
    target = trusted_path(target)
    trusted_path(bundle)
    for name in MODULES: trusted_path(bundle / name)
    check_bundle(bundle)
    identities = json.loads(trusted_path(identities_path).read_text()) if identities_path else {}
    original = config_path.read_bytes()
    result = merged_config(json.loads(original), identities)
    approvals = Path(result['component_approvals']); approvals.mkdir(mode=0o700, parents=True, exist_ok=True)
    trusted_path(approvals)
    # Keep the first config backup; reruns preserve identities and unknown fields.
    backup = config_path.with_suffix('.json.pre-component')
    if not backup.exists():
        with backup.open('xb') as out: out.write(original)
        backup.chmod(0o600)
    install_modules(bundle, target)
    fd, temporary = tempfile.mkstemp(prefix='.chejin-config-', dir=config_path.parent)
    with os.fdopen(fd, 'w') as out: json.dump(result, out)
    os.chmod(temporary, 0o600); os.replace(temporary, config_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'check', 'upgrade'])
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--identities', type=Path)
    args = parser.parse_args()
    if args.action == 'list': print(' '.join(MODULES)); return
    if args.action == 'check':
        check_bundle(args.bundle); return
    if os.geteuid() != 0: raise ValueError('OPERATOR_ROOT_REQUIRED')
    upgrade_tools(args.bundle, '/opt/chejin-formal-release', '/etc/chejin-formal-release.json', args.identities)
    print('OPERATOR_TOOLS_UPDATED_NO_APPLICATION_DEPLOY')


if __name__ == '__main__': main()

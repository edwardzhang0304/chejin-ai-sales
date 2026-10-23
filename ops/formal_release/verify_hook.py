"""Reuse the shipped updater and shared secure decoder for Hook publication."""
import json
from pathlib import Path

from verify import client_api, digest, require, verified_package_files
from component_release import canonical, sha


def verify_hook(folder, descriptor, approval, config):
    old = approval['old_client']
    # Installed operator baseline is independent of candidate/CI assertions.
    pinned = config['client_baseline_identities'][old['version']]
    require(all(old[k] == pinned[k] for k in ('version', 'zip_sha256', 'exe_sha256', 'updater_sha256')),
            'INSTALLED_BASELINE_IDENTITY_MISMATCH')
    model, contract = client_api(config, old['version'])
    release = model.from_api({**descriptor, 'update_available': True, 'latest_version': descriptor['version']})
    contract.validate_release_contract(release, current_version=old['version'],
                                       require_download_url=False, expected_channel='gray-hook')
    contract.verify_release_signature(release, trusted_keys=contract.load_trusted_release_keys(Path(config['public_keys'])))
    archive = folder / 'artifact.zip'
    require(archive.stat().st_size == release.artifact_size_bytes and digest(archive) == release.artifact_sha256,
            'ARTIFACT_HASH_MISMATCH')
    with verified_package_files(archive) as package:
        manifest = contract.verify_staged_package(release, package)
        actual = json.loads((package / '_internal/contracts/c2_contract_v3.json').read_text())
        after = approval['component_plan']['after']
        require(actual['contract_revision'] == after['contract_revision'] and sha(canonical(actual)) == after['contract_sha256'],
                'PACKAGED_CONTRACT_PLAN_MISMATCH')
        # Interpret actual frozen resources through the OLD installed rules,
        # never import code from the new archive or trust its capability claim.
        from chejin_worker_client.shared_rules import contract_rules
        frozen = {rev: json.loads((package / ('_internal/contracts/recovery/c2_contract_v3_' + rev + '.json')).read_text())
                  for rev, _ in contract_rules.RELEASED_READ_CONTRACTS}
        expected = contract_rules.recovery_contract_capability(actual, frozen)
        require(manifest.get('pending_read_recovery') == expected, 'PACKAGED_RECOVERY_MISMATCH')
        return {'contract_revision': actual['contract_revision'], 'contract_sha256': sha(canonical(actual)),
                'file_count': len(manifest['files']), 'package': 'passed', 'old_client_signature': 'passed'}


def trusted_path(path):
    """Operator staging/config/evidence: root-owned, no writable ancestry/links.

    Transfer must finish into a sealed operator directory first. This is never
    a CI-writable inbox or an approval bundled inside the downloaded ZIP.
    """
    path = Path(path)
    require(path.is_absolute(), 'OPERATOR_PATH_NOT_ABSOLUTE')
    for node in (path, *path.parents):
        info = node.lstat()
        require(not node.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
                'UNTRUSTED_OPERATOR_PATH')
    return path

"""Root-owned, bounded capacity preparation for formal builds only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

from verify import digest, identity, require, SHA, VERSION

GIB = 1024 ** 3
MIB = 1024 ** 2
FLOOR = 4 * GIB


def budget(config):
    policy = config.get('capacity', {})
    result = {k: policy.get(k, default) for k, default in (
        ('package_bytes', 512 * MIB), ('runtime_bytes', 512 * MIB), ('margin_bytes', 256 * MIB))}
    require(all(type(v) is int and 0 < v <= 4 * GIB for v in result.values()), 'INVALID_CAPACITY_BUDGET')
    require(result['package_bytes'] <= GIB, 'INVALID_CAPACITY_BUDGET')
    return result


def regular(path):
    return not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1


def safe_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.exists() and path.is_dir()
            and all(not p.is_symlink() for p in (path, *path.parents)), 'UNSAFE_CAPACITY_DIRECTORY')
    return path


def used(root):
    return sum(p.stat().st_size for p in root.rglob('*') if regular(p))


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=180).strip()


def inventory():
    return {
        'containers': sorted(run(['docker', 'ps', '-aq']).splitlines()),
        'images': sorted(run(['docker', 'image', 'ls', '--no-trunc', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}']).splitlines()),
        'volumes': sorted(run(['docker', 'volume', 'ls', '-q']).splitlines()),
    }


def write_receipt(root, receipt):
    path = root / 'capacity-last.json'
    require(not path.is_symlink(), 'UNSAFE_CAPACITY_RECEIPT')
    temp = root / 'capacity-last.tmp'
    # Do not follow a stale or attacker-supplied link.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(receipt, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def clean_duplicates(root, public, protected, receipt):
    from receiver import stage_id
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not SHA.fullmatch(folder.name):
            continue
        metadata = folder / 'metadata.json'
        verified_path = folder / 'verified.json'
        if not regular(metadata) or not regular(verified_path):
            continue  # Never remove an incomplete/retryable transfer.
        meta = json.loads(metadata.read_text())
        stem, _ = identity(meta)
        require(stage_id(meta) == folder.name, 'CAPACITY_STAGE_IDENTITY_MISMATCH')
        if meta['version'] in protected:
            continue
        verified = json.loads(verified_path.read_text())
        require(verified.get('package') == 'passed'
                and all(verified.get(k) == meta[k] for k in ('version', 'commit', 'sha256')),
                'CAPACITY_VERIFICATION_MISMATCH')
        source = folder / (stem + '.zip')
        target_dir = public / 'releases' / meta['version']
        if not source.exists() or not target_dir.exists():
            continue
        safe_directory(target_dir)
        target = target_dir / source.name
        if not regular(source) or not regular(target):
            continue
        expected = meta['files'][source.name]
        require(source.stat().st_size == target.stat().st_size == expected['size']
                and digest(source) == digest(target) == expected['sha256'], 'CAPACITY_PUBLIC_COPY_MISMATCH')
        item = {'version': meta['version'], 'stage_id': folder.name, 'file': source.name,
                'sha256': expected['sha256'], 'bytes': expected['size'], 'public_copy_retained': True}
        # Persist intent first; old metadata and public bytes remain recoverable.
        receipt['planned'].append(item)
        write_receipt(root, receipt)
        source.unlink()
        receipt['removed'].append(item)
        write_receipt(root, receipt)


def prepare(config, current_version, target_version):
    root = safe_directory(config['staging_root'])
    public = safe_directory(config['manual_download_root'])
    require(VERSION.fullmatch(current_version or '') and VERSION.fullmatch(target_version or ''), 'INVALID_CAPACITY_VERSION')
    policy = config.get('capacity', {})
    registered = policy.get('protected_versions', [])
    require(isinstance(registered, list) and registered and all(isinstance(v, str) and VERSION.fullmatch(v) for v in registered),
            'CAPACITY_PROTECTED_BASELINES_REQUIRED')
    active = run(['docker', 'exec', config['container'], 'python', '-c',
                  'from app.contracts.c2 import contract_revision;print(contract_revision())'])
    require(VERSION.fullmatch(active), 'INVALID_ACTIVE_CONTRACT')
    protected = set(registered) | {active, current_version, target_version}
    b = budget(config)
    required = FLOOR + sum(b.values())
    quota = config.get('staging_limit_bytes', 4 * GIB)
    receipt = {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
               'current_version': current_version, 'target_version': target_version,
               'protected_versions': sorted(protected), 'budget': b, 'required_free_bytes': required,
               'before_free_bytes': shutil.disk_usage(root).free, 'before_staging_bytes': used(root),
               'planned': [], 'removed': [], 'cache_cleanup': 'not_needed', 'status': 'preparing'}
    write_receipt(root, receipt)
    try:
        clean_duplicates(root, public, protected, receipt)
        if shutil.disk_usage(root).free < required:
            before = inventory()
            run(['docker', 'builder', 'prune', '--all', '--keep-storage', '256MB', '--force'])
            require(inventory() == before, 'CAPACITY_PROTECTED_DOCKER_OBJECTS_CHANGED')
            receipt['cache_cleanup'] = 'unused_only_keep_256MB'
        receipt['after_free_bytes'] = shutil.disk_usage(root).free
        receipt['after_staging_bytes'] = used(root)
        require(receipt['after_free_bytes'] >= required, 'CAPACITY_FREE_SPACE_INSUFFICIENT')
        require(receipt['after_staging_bytes'] + b['package_bytes'] <= quota, 'CAPACITY_STAGING_QUOTA_INSUFFICIENT')
        receipt['status'] = 'passed'
        return {'capacity_prepared': True, 'required_free_bytes': required,
                'free_bytes': receipt['after_free_bytes'], 'staging_bytes': receipt['after_staging_bytes'],
                'protected_versions': sorted(protected), 'removed_duplicates': len(receipt['removed'])}
    except Exception:
        receipt['status'] = 'failed'
        raise
    finally:
        write_receipt(root, receipt)
        audit = root / 'capacity-audit.jsonl'
        fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(json.dumps(receipt) + '\n')
            out.flush()
            os.fsync(out.fileno())


def check_transfer(config, total, remaining=None):
    """Actual artifact must fit the budget before any missing bytes are uploaded."""
    b = budget(config)
    require(total <= b['package_bytes'], 'CAPACITY_ARTIFACT_EXCEEDS_BUDGET')
    require(shutil.disk_usage(config['staging_root']).free > FLOOR + (total if remaining is None else remaining) + b['margin_bytes'],
            'CAPACITY_TRANSFER_HEADROOM_INSUFFICIENT')

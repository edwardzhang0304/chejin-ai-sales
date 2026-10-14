"""Validate a complete, offline Windows CPython 3.12 x64 dependency closure.

Cross-platform pip may evaluate dependency markers on the builder's OS. This
check evaluates every wheel's metadata using the target Windows environment,
including transitive conditional dependencies, before producing a hashed lock.
It does not import native modules or claim a Windows runtime test.
"""
from pathlib import Path
from email.parser import BytesParser
import hashlib
import json
import zipfile
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import cpython_tags, compatible_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def inspect_wheels(directory):
    supported = set(cpython_tags((3, 12), ['cp312'], ['win_amd64']))
    supported.update(compatible_tags((3, 12), 'cp312', ['win_amd64']))
    packages = {}
    for path in sorted(Path(directory).glob('*.whl')):
        name, version, _, tags = parse_wheel_filename(path.name)
        if not tags & supported:
            raise ValueError('WINDOWS_WHEEL_TAG_MISMATCH:' + path.name)
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA') and n.count('/') == 1]
            if len(names) != 1:
                raise ValueError('WINDOWS_WHEEL_METADATA_INVALID:' + path.name)
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        key = canonicalize_name(metadata['Name'])
        if key != name or str(version) != metadata['Version'] or key in packages:
            raise ValueError('WINDOWS_WHEEL_IDENTITY_INVALID:' + path.name)
        python = metadata.get('Requires-Python')
        if python and '3.12.10' not in SpecifierSet(python):
            raise ValueError('WINDOWS_WHEEL_PYTHON_MISMATCH:' + path.name)
        packages[key] = {'name': metadata['Name'], 'version': str(version),
                         'path': path, 'requires': metadata.get_all('Requires-Dist', [])}
    return packages


def target_closure(directory, requirements):
    packages = inspect_wheels(directory)
    environment = default_environment()
    environment.update(os_name='nt', sys_platform='win32', platform_system='Windows',
                       platform_machine='AMD64', python_version='3.12',
                       python_full_version='3.12.10', implementation_name='cpython',
                       platform_python_implementation='CPython', extra='')
    queue = [Requirement(s.strip()) for s in requirements if s.strip() and not s.startswith('#')]
    used, visited, missing = {}, set(), set()
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        key = canonicalize_name(requirement.name)
        package = packages.get(key)
        if package is None:
            missing.add(str(requirement).split(';')[0].strip())
            continue
        if package['version'] not in requirement.specifier:
            raise ValueError('WINDOWS_WHEEL_VERSION_MISMATCH:' + str(requirement))
        used[key] = package
        extras = {''} | set(requirement.extras)
        for extra in extras:
            if (key, extra) in visited:
                continue
            visited.add((key, extra))
            for spec in package['requires']:
                dependency = Requirement(spec)
                if dependency.marker and not dependency.marker.evaluate(dict(environment, extra=extra)):
                    continue
                # Already evaluated using the requesting package's extras.
                dependency.marker = None
                queue.append(dependency)
    return used, sorted(missing)


def write_lock(directory, requirements, lock, report):
    used, missing = target_closure(directory, requirements)
    if missing:
        raise ValueError('WINDOWS_WHEEL_DEPENDENCIES_MISSING:' + ','.join(missing))
    lines, records = [], []
    for key, package in sorted(used.items()):
        digest = hashlib.sha256(package['path'].read_bytes()).hexdigest()
        lines.append(key + '==' + package['version'] + ' --hash=sha256:' + digest)
        records.append({'name': key, 'version': package['version'],
                        'wheel': package['path'].name, 'sha256': digest})
    Path(lock).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    result = {'target': 'CPython 3.12 x64 Windows', 'windows_markers_checked': True,
              'requires_python_checked': True, 'compatible_wheel_tags_checked': True,
              'complete_dependency_closure': True, 'wheel_count': len(records),
              'actual_windows_runtime_tested': False, 'packages': records}
    Path(report).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result

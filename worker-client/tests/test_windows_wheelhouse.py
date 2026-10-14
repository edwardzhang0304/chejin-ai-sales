"""Delivery gate regressions: Windows markers and offline wheel completeness."""
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest

spec = importlib.util.spec_from_file_location('windows_wheelhouse', Path(__file__).parents[1] / 'scripts/windows_wheelhouse.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def wheel(root, name, version='1.0', tag='py3-none-any', requires=(), python='>=3.8', vendored=False):
    path = root / f'{name}-{version}-{tag}.whl'
    metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: {python}\n'
    metadata += ''.join(f'Requires-Dist: {r}\n' for r in requires)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{version}.dist-info/METADATA', metadata)
        if vendored:
            archive.writestr('vendor/other-1.0.dist-info/METADATA', 'Name: other\nVersion: 1.0')
    return path


def test_windows_only_transitive_dependency_cannot_be_skipped(tmp_path):
    wheel(tmp_path, 'app', requires=['windowshelper>=1; sys_platform == "win32"', 'machelper; sys_platform == "darwin"'])
    used, missing = module.target_closure(tmp_path, ['app==1.0'])
    assert missing == ['windowshelper>=1']
    assert list(used) == ['app']


def test_complete_hashed_lock_uses_target_not_host(tmp_path):
    wheel(tmp_path, 'app', requires=['windowshelper; platform_system == "Windows"'])
    wheel(tmp_path, 'windowshelper', tag='cp312-cp312-win_amd64')
    lock, report = tmp_path / 'lock.txt', tmp_path / 'report.json'
    result = module.write_lock(tmp_path, ['app==1.0'], lock, report)
    assert result['wheel_count'] == 2
    assert result['actual_windows_runtime_tested'] is False
    assert all(' --hash=sha256:' in line for line in lock.read_text().splitlines())
    assert json.loads(report.read_text())['complete_dependency_closure']


def test_missing_pinned_pyperclip_blocks_package(tmp_path):
    wheel(tmp_path, 'pyperclip', '1.11.0')
    with pytest.raises(ValueError, match='VERSION_MISMATCH'):
        module.write_lock(tmp_path, ['pyperclip==1.9.0'], tmp_path/'lock', tmp_path/'report')
    assert not (tmp_path/'lock').exists()


@pytest.mark.parametrize('tag', ['cp312-cp312-macosx_11_0_arm64', 'cp313-cp313-win_amd64', 'cp312-cp312-win32'])
def test_wrong_os_python_or_architecture_rejected(tmp_path, tag):
    wheel(tmp_path, 'app', tag=tag)
    with pytest.raises(ValueError, match='TAG_MISMATCH'):
        module.target_closure(tmp_path, ['app'])


def test_required_python_version_rejected(tmp_path):
    wheel(tmp_path, 'app', python='>=3.13')
    with pytest.raises(ValueError, match='PYTHON_MISMATCH'):
        module.target_closure(tmp_path, ['app'])


def test_abi3_and_bundled_vendor_metadata_are_supported(tmp_path):
    wheel(tmp_path, 'app', tag='cp38-abi3-win_amd64', vendored=True)
    used, missing = module.target_closure(tmp_path, ['app'])
    assert not missing and list(used) == ['app']


def test_optional_extra_dependencies_only_when_requested(tmp_path):
    wheel(tmp_path, 'app', requires=['optional; extra == "feature"'])
    assert module.target_closure(tmp_path, ['app'])[1] == []
    assert module.target_closure(tmp_path, ['app[feature]'])[1] == ['optional']


def test_missing_closure_never_writes_installable_lock(tmp_path):
    wheel(tmp_path, 'app', requires=['missing'])
    with pytest.raises(ValueError, match='DEPENDENCIES_MISSING'):
        module.write_lock(tmp_path, ['app'], tmp_path/'lock', tmp_path/'report')
    assert not (tmp_path/'lock').exists()

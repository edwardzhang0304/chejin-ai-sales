"""Execute the actual build gate/manifest expressions, without building an EXE."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

BUILD = Path(__file__).resolve().parents[1] / 'scripts' / 'build-windows.ps1'


@pytest.fixture
def powershell():
    executable = os.environ.get('CHEJIN_TEST_POWERSHELL') or shutil.which('powershell.exe') or shutil.which('pwsh')
    if not executable:
        pytest.skip('PowerShell runtime required; this is not a Windows EXE build test')
    return executable


def local_ocr(ok=True):
    return {'name': 'vision_ocr_subprocess', 'ok': ok}


def vision(configured=False, source='worker_backend'):
    return {'name': 'vision_credential', 'ok': True,
            'detail': {'credential_source': source, 'configured': configured}}


def run_gate(powershell, tmp_path, transport, checks, development=False, local_probe_exit=0):
    source = BUILD.read_text(encoding='utf-8-sig')
    begin = source.index('$BundledVisionOcrProbe = Start-Process')
    end = source.index('if (@(Get-ChildItem -Path $PackageDir -Recurse -File -Filter "vision-runtime.json")', begin)
    gate = source[begin:end]
    manifest_lines = '\n'.join(line for line in source.splitlines() if line.startswith('  vision_'))
    assert 'vision_credential_embedded' in manifest_lines
    report = tmp_path / 'packaged-vision-preflight.json'
    report.write_text(json.dumps({'ok': True, 'checks': checks}), encoding='utf-8')
    script = tmp_path / 'gate.ps1'
    script.write_text('''param([string]$Transport, [string]$Report, [string]$Development, [int]$LocalProbeExit)
$ErrorActionPreference = "Stop"
$ReportsDir = Split-Path -Parent $Report
$ExePath = "not-executed.exe"
$PackagingDiagnosticPath = Join-Path $ReportsDir "absent-diagnostic.jsonl"
$script:Probes = @()
function Start-Process {
  param([string]$FilePath, [string[]]$ArgumentList, [switch]$Wait, [switch]$PassThru)
  $script:Probes += $ArgumentList[0]
  if ($ArgumentList[0] -eq "--omniauto-ocr-probe") { return [pscustomobject]@{ExitCode=$LocalProbeExit} }
  if ($ArgumentList[0] -eq "--preflight") { return [pscustomobject]@{ExitCode=0} }
  throw "Unexpected executable invocation"
}
$DevelopmentBuild = $Development -eq "true"
''' + gate + '\n$Manifest = [ordered]@{\n' + manifest_lines + '\n}\n$Manifest["test_probe_calls"] = $script:Probes\n$Manifest | ConvertTo-Json -Compress\n', encoding='utf-8-sig')
    return subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(script),
                           transport, str(report), str(development).lower(), str(local_probe_exit)],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('transport,checks,accepted', [
    ('hook', [local_ocr()], True),
    ('hook', [local_ocr(), vision()], False),
    ('hook', [local_ocr(), vision(), vision()], False),
    ('hook', [], True),
    ('ocr', [local_ocr(), vision()], True),
    ('ocr', [local_ocr()], False),
    ('ocr', [local_ocr(), vision(True)], False),
    ('ocr', [local_ocr(), vision(source='embedded')], False),
    ('ocr', [local_ocr(), vision(), vision()], False),
])
def test_actual_packaged_preflight_gate(powershell, tmp_path, transport, checks, accepted):
    result = run_gate(powershell, tmp_path, transport, checks)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if accepted:
        manifest = json.loads(result.stdout)
        assert manifest['vision_credential_embedded'] is False
        assert manifest['test_probe_calls'] == ['--omniauto-ocr-probe', '--preflight']
        assert manifest['vision_configuration_locked'] is True
        assert manifest['vision_credential_source'] == ('not_required' if transport == 'hook' else 'worker_backend')
        assert manifest['vision_live_probe_check'] == ('not_applicable' if transport == 'hook' else 'runtime_after_binding')
        if transport == 'hook':
            for field in ('vision_provider', 'vision_base_url', 'vision_model', 'vision_request_style'):
                assert manifest[field] is None


def test_development_hook_metadata_does_not_claim_vision_dependency(powershell, tmp_path):
    result = run_gate(powershell, tmp_path, 'hook', [local_ocr()], development=True)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest['vision_credential_source'] == 'not_required'
    assert manifest['vision_live_probe_check'] == 'not_applicable'
    assert manifest['vision_credential_embedded'] is False


def test_complete_build_script_parses_without_execution(powershell, tmp_path):
    script = tmp_path / 'parse.ps1'
    script.write_text('''param([string]$Source)
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw "Build script syntax invalid" }
''', encoding='utf-8-sig')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(script), str(BUILD)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('transport', ['hook', 'ocr'])
def test_failed_local_ocr_probe_still_blocks_the_package(powershell, tmp_path, transport):
    checks = [] if transport == 'hook' else [vision()]
    result = run_gate(powershell, tmp_path, transport, checks, local_probe_exit=1)
    assert result.returncode != 0
    assert 'OCR' in result.stderr

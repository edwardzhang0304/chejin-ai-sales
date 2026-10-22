"""Check the new offline entries stay Hook-only and fail closed before sealing.

The formal Windows build additionally executes the actual PowerShell validator
on the copied script; this check alone does not certify native Windows syntax.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_component_entries_are_hook_only_and_validated_before_sealing():
    build = (ROOT / 'worker-client/scripts/build-windows.ps1').read_text(encoding='utf-8-sig')
    start = build.index('if ($Transport -eq "hook") {\n  foreach ($ComponentEntry')
    end = build.index('$UpdatePackageManifestJson', start)
    hook = build[start:end]
    for name in ('update-hook-component.ps1', 'Update-CheJin-Hook.cmd', 'Rollback-CheJin-Hook.cmd'):
        assert (ROOT / 'worker-client/packaging' / name).is_file()
        assert name in hook and name not in build[:start] and name not in build[end:]
    assert "-Destination (Join-Path $PackageDir $ComponentEntry)" in hook
    validation = hook.index('-File $UatLauncherValidatorPath -ScriptPath')
    failure = hook.index("if ($LASTEXITCODE -ne 0) { throw 'Hook component maintenance script failed syntax verification' }")
    assert validation < failure
    assert "(Join-Path $PackageDir 'update-hook-component.ps1')" in hook[validation:failure]
    assert (ROOT / 'worker-client/packaging/update-hook-component.ps1').read_bytes().startswith(b'\xef\xbb\xbf')

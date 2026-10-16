# Run as the normal Worker user. Only the external recorder requests elevation.
$ErrorActionPreference = 'Stop'
$cache = Join-Path $env:LOCALAPPDATA 'CheJinWorkerUpdate'
if (-not (Test-Path -LiteralPath (Join-Path $cache 'requests') -PathType Container)) { throw 'UPDATE_CACHE_MISSING' }
$descriptor = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'trace-files.json') -Raw | ConvertFrom-Json
foreach ($property in $descriptor.files.PSObject.Properties) {
    $path = Join-Path $PSScriptRoot $property.Name
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value) { throw 'DIAGNOSTIC_FILE_CHANGED' }
}
$output = Join-Path $PSScriptRoot ('trace-report-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $output | Out-Null
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'active-trace.json'), (@{output=$output} | ConvertTo-Json), $utf8)
Write-Host 'This external diagnostic requests administrator permission. Worker must stay non-administrator.'
Write-Host 'It does not repair, launch or modify Worker. Only update-cache metadata is saved.'
$arguments = '--collect "' + $cache + '" "' + $output + '" 1200 600'
$process = Start-Process -FilePath (Join-Path $PSScriptRoot 'CheJinUpdateTrace.exe') -ArgumentList $arguments -Verb RunAs -PassThru
$process.WaitForExit()
# Allow the independent watchdog to record its own session cleanup.
Start-Sleep -Seconds 2
$zip = $output + '.zip'
Compress-Archive -Path (Join-Path $output '*') -DestinationPath $zip
Write-Host ('Please send this diagnostic ZIP: ' + [IO.Path]::GetFileName($zip))
Write-Host 'This is evidence, not a claim that upgrade was repaired.'

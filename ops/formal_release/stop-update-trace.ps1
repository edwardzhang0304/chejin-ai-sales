$ErrorActionPreference = 'Stop'
$active = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'active-trace.json') -Raw | ConvertFrom-Json
$output = [IO.Path]::GetFullPath([string]$active.output)
if ([IO.Path]::GetDirectoryName($output) -ne $PSScriptRoot -or [IO.Path]::GetFileName($output) -notmatch '^trace-report-[0-9a-f]{32}$') { throw 'UNRECOGNIZED_DIAGNOSTIC_DIRECTORY' }
if ((Get-Item -LiteralPath $output).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'DIAGNOSTIC_LINK_NOT_ALLOWED' }
[IO.File]::WriteAllText((Join-Path $output 'stop.request'), '')
Write-Host 'Stop requested. The START window will produce the diagnostic ZIP.'

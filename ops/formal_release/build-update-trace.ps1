param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$Destination = (Resolve-Path -LiteralPath $Destination).Path
$packages = @(
    @('microsoft.diagnostics.tracing.traceevent','2.0.77','0055b65a090d4e7b179bd32fc9365bdcc1f70f7f3d081685f93b2067436c0850','lib/net45/'),
    @('system.runtime.compilerservices.unsafe','4.5.2','f1e5175c658ed8b2fbb804cc6727b6882a503844e7da309c8d4846e9ca11e4ef','lib/netstandard1.0/')
)
foreach ($package in $packages) {
    $archive = Join-Path $env:RUNNER_TEMP ($package[0] + '.nupkg')
    $url = 'https://api.nuget.org/v3-flatcontainer/{0}/{1}/{0}.{1}.nupkg' -f $package[0],$package[1]
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $package[2]) { throw 'OFFICIAL_DEPENDENCY_HASH_CHANGED' }
    $z = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        foreach ($entry in $z.Entries) {
            if ($entry.FullName.StartsWith($package[3]) -and $entry.Name.EndsWith('.dll')) {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,(Join-Path $Destination $entry.Name),$false)
            }
        }
    } finally { $z.Dispose() }
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$assembly = Join-Path $Destination 'Microsoft.Diagnostics.Tracing.TraceEvent.dll'
$serialization = Join-Path $Destination 'Microsoft.Diagnostics.FastSerialization.dll'
& $compiler /nologo /target:exe /optimize+ /platform:anycpu "/out:$Destination/CheJinUpdateTrace.exe" "/reference:$assembly" "/reference:$serialization" /reference:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'update-access-trace.cs')
if ($LASTEXITCODE -ne 0) { throw 'DIAGNOSTIC_COMPILATION_FAILED' }
foreach ($name in @('start-update-trace.ps1','stop-update-trace.ps1','Start-CheJinUpdateTrace.cmd','Stop-CheJinUpdateTrace.cmd','update-trace-licenses.txt')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $Destination
}
$files = @{}
Get-ChildItem -LiteralPath $Destination -File | ForEach-Object { $files[$_.Name] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
$inputs = @{}
foreach ($name in @('update-access-trace.cs','build-update-trace.ps1','start-update-trace.ps1','stop-update-trace.ps1','Start-CheJinUpdateTrace.cmd','Stop-CheJinUpdateTrace.cmd','update-trace-licenses.txt')) {
    $inputs[$name] = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText((Join-Path $Destination 'trace-files.json'), (@{files=$files;tool_inputs=$inputs;not_a_worker_package=$true;collector_source_sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'update-access-trace.cs') -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))

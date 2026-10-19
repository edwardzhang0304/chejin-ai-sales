param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Prepare','CopyDll','OpenWorker')][string]$Stage,
    [string]$WeChatExe = ''
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $root 'bundle-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$worker = Join-Path $root 'runtime\CheJinWorkerClient'
$profilePath = Join-Path $worker 'profile.json'
$portable = $manifest.installation_mode -eq 'portable_new_pc_test'
if ($portable -and $manifest.wechat_executable -ne 'WeChat/WeChat.exe') { throw 'Unexpected portable executable path.' }
$dllRelative = if ($portable) { 'WeChat/version.dll' } else { 'hook/version.dll' }
$dll = Join-Path $root $dllRelative
$dllHash = $manifest.files.PSObject.Properties[$dllRelative].Value

function Assert-Hash([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Symbolic links are not supported.' }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) { throw "File verification failed: $Path" }
}
function Assert-Idle {
    if (Get-Process -Name WeChat,Weixin,CheJinWorkerClient,CheJinUpdater -ErrorAction SilentlyContinue) {
        throw 'Normally exit WeChat and Worker before preparing this installation. No process was terminated.'
    }
}
function Assert-FreshWorkerHome {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is missing.' }
    if (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'CheJinWorkerHook')) {
        throw 'This Windows user already has Hook Worker data. Stop and contact operations; nothing was reset. For an already completed installation use the desktop shortcut.'
    }
}
function Assert-Runtime {
    $ready = Get-Content -LiteralPath (Join-Path $root 'runtime-ready.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $entry = @($manifest.files.PSObject.Properties | Where-Object { $_.Name -like 'worker/*' })
    if ($entry.Count -ne 1 -or $ready.worker_zip_sha256 -ne $entry[0].Value -or
        $ready.worker_manifest_sha256 -ne $manifest.worker_manifest_sha256) { throw 'Runtime preparation receipt mismatch.' }
    $fileManifest = Join-Path $worker 'update-package-manifest.json'
    Assert-Hash $fileManifest $manifest.worker_manifest_sha256
    $installed = Get-Content -LiteralPath $fileManifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installed.version -ne $manifest.product_version -or $installed.transport -ne 'hook') { throw 'Worker identity mismatch.' }
    foreach ($file in $installed.files.PSObject.Properties) {
        Assert-Hash (Join-Path $worker $file.Name) $file.Value
    }
}
function Assert-WeChat([string]$Path) {
    $f = Get-Item -LiteralPath $Path
    if ($f.Name -ine 'WeChat.exe' -or ($f.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Select the installed WeChat.exe.' }
    if ($f.VersionInfo.FileVersion -ne $manifest.expected_installed_wechat_file_version) { throw 'WeChat version mismatch. Expected 3.9.10.16.' }
    $stream = [IO.File]::Open($f.FullName, 'Open', 'Read', 'Read')
    $reader = New-Object IO.BinaryReader($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5a4d) { throw 'Invalid WeChat executable.' }
        $stream.Position = 0x3c; $offset = $reader.ReadInt32()
        if ($offset -lt 0 -or $offset -gt $stream.Length - 6) { throw 'Invalid PE header.' }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne 0x14c) { throw 'WeChat must be x86 / 32-bit.' }
    } finally { $reader.Dispose(); $stream.Dispose() }
}
function Get-FreePort {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $listener.ExclusiveAddressUse = $true
    try { $listener.Start(); return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
function Assert-PortFree([int]$Port) {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
    $listener.ExclusiveAddressUse = $true
    try { $listener.Start() } finally { $listener.Stop() }
}
function Load-Profile {
    $p = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($p.schema_version -ne 1 -or $p.api_base_url -ne 'https://jiangsuchejin.com/api') { throw 'Deployment configuration mismatch.' }
    if ($p.deployment.dll_sha256 -ne $dllHash) { throw 'DLL configuration mismatch.' }
    if ($portable -and $p.deployment.wechat_exe -ne (Join-Path $root $manifest.wechat_executable)) { throw 'Portable directory moved after preparation. Stop and contact operations; no data was reset.' }
    Assert-WeChat $p.deployment.wechat_exe
    Assert-Hash (Join-Path (Split-Path $p.deployment.wechat_exe) 'version.dll') $p.deployment.dll_sha256
    if ($p.deployment.hook_api_port -lt 1 -or $p.deployment.hook_api_port -gt 65535 -or
        $p.deployment.receiver_port -lt 1 -or $p.deployment.receiver_port -gt 65535 -or
        $p.deployment.hook_api_port -eq $p.deployment.receiver_port) { throw 'Invalid local ports.' }
    return $p
}
function Get-SetupUserContext {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $session = (Get-Process -Id $PID).SessionId
    $owners = @()
    foreach ($desktop in @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe' AND SessionId=$session" -ErrorAction Stop)) {
        $owner = Invoke-CimMethod -InputObject $desktop -MethodName GetOwnerSid -ErrorAction Stop
        if ($owner.ReturnValue -ne 0 -or -not $owner.Sid) { throw 'Cannot verify the desktop user. No installation was performed.' }
        $owners += [string]$owner.Sid
    }
    $policy = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -ErrorAction Stop
    return [pscustomobject]@{
        UserSid = $identity.User.Value; SessionId = $session; DesktopSids = $owners
        Elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        EnableLUA = $policy.EnableLUA; FilterAdministratorToken = $policy.FilterAdministratorToken
    }
}
function Assert-SetupUserContext {
    $context = Get-SetupUserContext
    $owners = @($context.DesktopSids | Select-Object -Unique)
    if ($context.SessionId -le 0 -or -not $context.UserSid -or $owners.Count -ne 1 -or $owners[0] -ne $context.UserSid) {
        throw 'Run from the signed-in desktop user. Cannot install for another user or an unverified desktop.'
    }
    if ($context.Elevated) {
        # A disabled UAC or an unfiltered built-in Administrator has no normal
        # desktop token to switch back to. Keep that user's configuration; never
        # change UAC, switch accounts, or request elevation for the whole setup.
        $automaticAdmin = ($null -ne $context.EnableLUA -and $context.EnableLUA -eq 0) -or
            ($context.EnableLUA -eq 1 -and $context.UserSid.EndsWith('-500') -and
             ($null -eq $context.FilterAdministratorToken -or $context.FilterAdministratorToken -eq 0))
        if (-not $automaticAdmin) { throw 'Run normally from File Explorer, not as administrator. Only a protected-folder DLL copy may request UAC.' }
        Write-Host 'Windows runs this desktop as administrator by default. Continuing for the same signed-in user; system settings are unchanged.'
    }
}
try {
    if ($Stage -ne 'CopyDll') {
        Assert-SetupUserContext
    }
    Assert-Hash $dll $dllHash
    if ($portable) {
        foreach ($file in @($manifest.files.PSObject.Properties | Where-Object { $_.Name -like 'WeChat/*' })) {
            Assert-Hash (Join-Path $root $file.Name) $file.Value
        }
    }
    if ($Stage -eq 'Install') {
        Assert-Idle
        $entry = @($manifest.files.PSObject.Properties | Where-Object { $_.Name -like 'wechat/*' })
        if ($entry.Count -ne 1) { throw 'Installer manifest is invalid.' }
        $installer = Join-Path $root $entry[0].Name
        Assert-Hash $installer $entry[0].Value
        Write-Host 'Install this WeChat. Do not launch it from its ordinary shortcut. Exit its tray process before step 02.'
        $process = Start-Process -FilePath $installer -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Installer exited: $($process.ExitCode)" }
    } elseif ($Stage -eq 'CopyDll') {
        # The only elevated action: copy the pinned DLL to a checked, stopped WeChat.
        Assert-Idle; Assert-WeChat $WeChatExe
        $target = Join-Path (Split-Path $WeChatExe) 'version.dll'
        if (Test-Path -LiteralPath $target) { Assert-Hash $target $dllHash }
        else { Copy-Item -LiteralPath $dll -Destination $target -ErrorAction Stop }
        Assert-Hash $target $dllHash
    } elseif ($Stage -eq 'Prepare') {
        Assert-Idle; Assert-FreshWorkerHome
        if (-not (Test-Path -LiteralPath $worker)) {
            $runtime = Join-Path $root 'runtime'
            if (Test-Path -LiteralPath $runtime) { throw 'A partial runtime directory exists. Extract the complete bundle to a new folder; do not delete old Worker data.' }
            $entry = @($manifest.files.PSObject.Properties | Where-Object { $_.Name -like 'worker/*' })
            if ($entry.Count -ne 1) { throw 'Worker ZIP manifest is invalid.' }
            $zip = Join-Path $root $entry[0].Name
            Assert-Hash $zip $entry[0].Value
            Expand-Archive -LiteralPath $zip -DestinationPath $runtime
            if (-not (Test-Path -LiteralPath (Join-Path $worker 'CheJinWorkerClient.exe'))) { throw 'Worker extraction incomplete.' }
            # The marker is committed only after validating every original package file.
            $fileManifest = Join-Path $worker 'update-package-manifest.json'
            Assert-Hash $fileManifest $manifest.worker_manifest_sha256
            $installed = Get-Content -LiteralPath $fileManifest -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($file in $installed.files.PSObject.Properties) {
                Assert-Hash (Join-Path $worker $file.Name) $file.Value
            }
            @{worker_zip_sha256=$entry[0].Value;worker_manifest_sha256=$manifest.worker_manifest_sha256} |
                ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'runtime-ready.json') -Encoding UTF8
        } else {
            Assert-Runtime
        }
        if (Test-Path -LiteralPath $profilePath) { $profile = Load-Profile }
        else {
            if ($portable) { $WeChatExe = Join-Path $root $manifest.wechat_executable }
            else {
                Add-Type -AssemblyName System.Windows.Forms
                $picker = New-Object Windows.Forms.OpenFileDialog
                $picker.Title = 'Select WeChat.exe installed in step 01'
                $picker.Filter = 'WeChat.exe|WeChat.exe'
                if ($picker.ShowDialog() -ne 'OK') { throw 'Selection cancelled.' }
                $WeChatExe = $picker.FileName
            }
            Assert-WeChat $WeChatExe
            $target = Join-Path (Split-Path $WeChatExe) 'version.dll'
            if (Test-Path -LiteralPath $target) { Assert-Hash $target $dllHash }
            else {
                try { Copy-Item -LiteralPath $dll -Destination $target -ErrorAction Stop }
                catch [UnauthorizedAccessException] {
                    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Stage CopyDll -WeChatExe "' + $WeChatExe + '"'
                    $copy = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs -Wait -PassThru
                    if ($copy.ExitCode -ne 0) { throw 'DLL installation was cancelled or failed.' }
                }
            }
            Assert-Hash $target $dllHash
            $hookPort = Get-FreePort
            do { $receiverPort = Get-FreePort } while ($receiverPort -eq $hookPort)
            $profile = [ordered]@{schema_version=1;api_base_url='https://jiangsuchejin.com/api';deployment=[ordered]@{
                revision=('new-pc-' + [guid]::NewGuid().ToString());wechat_exe=$WeChatExe;
                dll_sha256=$dllHash;hook_api_port=$hookPort;receiver_port=$receiverPort}}
            $profile | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $profilePath -Encoding UTF8
            $profile = Load-Profile
        }
        Assert-PortFree $profile.deployment.hook_api_port
        Assert-PortFree $profile.deployment.receiver_port
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Work WeChat.lnk'))
        $link.TargetPath = $profile.deployment.wechat_exe
        $link.WorkingDirectory = Split-Path $profile.deployment.wechat_exe
        $link.Arguments = 'StartPort=' + $profile.deployment.hook_api_port + ' ConnectType=http RecvType=0'
        $link.Save()
        Write-Host 'Now double-click desktop Work WeChat. Enter the Hook activation code when prompted, then scan to log in.'
        Write-Host 'Turn OFF WeChat automatic voice transcription. Only then run Open-Worker. Worker has NOT been configured or started.'
    } else {
        Assert-FreshWorkerHome; Assert-Runtime
        $profile = Load-Profile
        if (Get-Process -Name CheJinWorkerClient,CheJinUpdater -ErrorAction SilentlyContinue) { throw 'Worker is already running. Do not start another instance.' }
        if (-not (Get-Process -Name WeChat -ErrorAction SilentlyContinue)) { throw 'Open desktop Work WeChat and log in before Open-Worker.' }
        Write-Host 'You must actually turn OFF automatic voice transcription before answering Y in the official setup.'
        & (Join-Path $worker 'Setup-CheJin-Hook.cmd')
        if ($LASTEXITCODE -ne 0) { throw 'Official Worker setup did not finish. Worker was not opened.' }
        $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CheJin Worker.lnk'
        if (-not (Test-Path -LiteralPath $shortcut)) { throw 'Worker shortcut is missing.' }
        Start-Process -FilePath $shortcut
        Write-Host 'Worker opened. Stop at its binding page or workbench. DO NOT click Start accepting.'
    }
} catch {
    Write-Host ('STOP: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

# Diagnostic only. No repair, upload, elevation, process termination or ACL changes.
$ErrorActionPreference = 'Stop'
$report = [ordered]@{
    schema_version = 1; tool = 'update-access-diagnostic'; started_utc = [DateTime]::UtcNow.ToString('o')
    result = 'diagnostic_only_not_an_upgrade_or_repair'; limitations = @(
        'Current observations cannot identify a past transient lock.',
        'Restart Manager checks regular files only; directory handles and filter drivers may be invisible.',
        'A successful synthetic rename does not prove the complete upgrade succeeds.'
    ); errors = @(); directories = @(); residual_requests = @(); rename_probe = @()
}
function Failure($exception) {
    while ($exception.InnerException) { $exception = $exception.InnerException }
    @{ type = $exception.GetType().Name; hresult = $exception.HResult; native_code = ($exception.HResult -band 65535) }
}
function Assert-PlainPath([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    while ($item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'REPARSE_PATH_NOT_INSPECTED' }
        if ($item -is [IO.FileInfo]) { $item = $item.Directory } else { $item = $item.Parent }
    }
}
function Directory-Facts([string]$path, [string]$label) {
    $facts = [ordered]@{ location = $label; exists = [IO.Directory]::Exists($path) }
    if (-not $facts.exists) { return $facts }
    try {
        Assert-PlainPath $path
        $item = Get-Item -LiteralPath $path -Force
        $acl = Get-Acl -LiteralPath $path
        $facts.attributes = [string]$item.Attributes
        $facts.inheritance_protected = $acl.AreAccessRulesProtected
        $facts.owner_is_current_user = ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $script:identity.User.Value)
        # Keep applicable rules, never user/group names, SIDs or full paths.
        $facts.current_token_rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
            if ($script:tokenSids -contains $_.IdentityReference.Value) {
                @{ type = [string]$_.AccessControlType; rights = [string]$_.FileSystemRights;
                   inherited = $_.IsInherited; inheritance = [string]$_.InheritanceFlags; propagation = [string]$_.PropagationFlags }
            }
        })
    } catch { $facts.error = Failure $_.Exception }
    return $facts
}
function Native-Path([string]$path) {
    if ($path.StartsWith('\\')) { return '\\?\UNC\' + $path.Substring(2) }
    return '\\?\' + $path
}
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class CheJinAccessNative {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool MoveFileExW(string from, string to, uint flags);
    public static int Rename(string from, string to) {
        return MoveFileExW(from, to, 1) ? 0 : Marshal.GetLastWin32Error();
    }
    [StructLayout(LayoutKind.Sequential)]
    struct UniqueProcess { public uint pid; public System.Runtime.InteropServices.ComTypes.FILETIME start; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct ProcessInfo {
        public UniqueProcess process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string app;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string service;
        public int type; public uint status; public uint session;
        [MarshalAs(UnmanagedType.Bool)] public bool restartable;
    }
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
    static extern int RmStartSession(out uint session, uint flags, StringBuilder key);
    [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
    static extern int RmRegisterResources(uint session, uint count, string[] files, uint apps, IntPtr processes, uint services, IntPtr names);
    [DllImport("rstrtmgr.dll")]
    static extern int RmGetList(uint session, out uint needed, ref uint count, [In,Out] ProcessInfo[] processes, ref uint reasons);
    [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint session);
    public static int[] FileUsers(string[] files) {
        uint session; int code = RmStartSession(out session, 0, new StringBuilder(33));
        if (code != 0) return new int[] {code};
        try {
            code = RmRegisterResources(session, (uint)files.Length, files, 0, IntPtr.Zero, 0, IntPtr.Zero);
            if (code != 0) return new int[] {code};
            uint needed, count = 0, reasons = 0;
            code = RmGetList(session, out needed, ref count, null, ref reasons);
            for (int attempt=0; code==234 && attempt<3 && needed<=1024; attempt++) {
                var info = new ProcessInfo[needed]; count = needed;
                code = RmGetList(session, out needed, ref count, info, ref reasons);
                if (code==0) {
                    var result = new int[count+1];
                    for (int i=0; i<count; i++) result[i+1] = (int)info[i].process.pid;
                    return result;
                }
            }
            return new int[] {code};
        } finally { RmEndSession(session); }
    }
}
'@
$owned = $null
$outputRoot = Join-Path $PSScriptRoot ('report-' + [guid]::NewGuid().ToString('N'))
try {
    $script:identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $script:tokenSids = @($identity.User.Value) + @($identity.Groups | ForEach-Object { $_.Value })
    $report.elevated = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $report.powershell_version = $PSVersionTable.PSVersion.ToString()
    if (-not $env:LOCALAPPDATA -or -not [IO.Path]::IsPathRooted($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA_UNAVAILABLE' }
    $cache = Join-Path $env:LOCALAPPDATA 'CheJinWorkerUpdate'
    $requests = Join-Path $cache 'requests'
    $report.directories += Directory-Facts $env:LOCALAPPDATA 'local_app_data'
    $report.directories += Directory-Facts $cache 'update_cache'
    $report.directories += Directory-Facts $requests 'update_requests'
    $report.related_processes = @(Get-Process -Name CheJinWorkerClient,CheJinUpdater,WeChat,Weixin,MsMpEng,MsSense -ErrorAction SilentlyContinue |
        ForEach-Object { @{ name = $_.ProcessName; pid = $_.Id } })
    if ([IO.Directory]::Exists($requests)) {
        Assert-PlainPath $requests
        $recent = @(Get-ChildItem -LiteralPath $requests -Directory -Force | Where-Object { $_.Name -like 'update-*' } |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 3)
        $index = 0
        foreach ($request in $recent) {
            $index++
            $entry = [ordered]@{ index = $index; directories = @(); file_users = @(); files_checked = 0; coverage = 'at_most_512_regular_files_per_staging_directory' }
            try {
                Assert-PlainPath $request.FullName
                $entry.directories += Directory-Facts $request.FullName 'request_root'
                foreach ($name in @('staging.extracting', 'staging')) {
                    $path = Join-Path $request.FullName $name
                    $entry.directories += Directory-Facts $path $name
                    if (-not [IO.Directory]::Exists($path)) { continue }
                    Assert-PlainPath $path
                    # Explicit traversal: never follow junctions or recursively scan business data.
                    $queue = New-Object 'System.Collections.Generic.Queue[string]'
                    $queue.Enqueue($path); $files = New-Object 'System.Collections.Generic.List[string]'; $visited = 0
                    while ($queue.Count -gt 0 -and $files.Count -lt 512 -and $visited -lt 1024) {
                        $dir = $queue.Dequeue(); Assert-PlainPath $dir; $visited++
                        foreach ($item in Get-ChildItem -LiteralPath $dir -Force) {
                            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
                            if ($item.PSIsContainer) { $queue.Enqueue($item.FullName) }
                            elseif ($files.Count -lt 512) { $files.Add($item.FullName) }
                        }
                    }
                    $entry.files_checked += $files.Count
                    if ($files.Count -gt 0) {
                        $users = [CheJinAccessNative]::FileUsers($files.ToArray())
                        $entry.restart_manager_code = $users[0]
                        foreach ($processId in @($users | Select-Object -Skip 1)) {
                            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
                            $entry.file_users += @{ pid = $processId; name = $(if ($process) { $process.ProcessName } else { 'exited_or_unknown' }) }
                        }
                    }
                }
            } catch { $entry.error = Failure $_.Exception }
            $report.residual_requests += $entry
        }
        # Probe only a new, uniquely owned sibling of real requests. Never rename real staging.
        $owned = Join-Path $requests ('diagnostic-' + [guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($owned) | Out-Null
        Assert-PlainPath $owned
        $source = Join-Path $owned 'staging.extracting'; $target = Join-Path $owned 'staging'
        [IO.Directory]::CreateDirectory($source) | Out-Null
        [IO.File]::WriteAllText((Join-Path $source 'synthetic.txt'), 'CheJin synthetic diagnostic; no customer data')
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            Assert-PlainPath $owned
            $code = [CheJinAccessNative]::Rename((Native-Path $source), (Native-Path $target))
            $report.rename_probe += @{ attempt = $attempt + 1; utc = [DateTime]::UtcNow.ToString('o'); winerror = $code }
            if ($code -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
    } else { $report.probe_skipped = 'original_requests_directory_missing_no_substitute_location_tested' }
    try {
        $report.defender_events = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Windows Defender/Operational'; Id = 1116,1117,1123,1124; StartTime = (Get-Date).AddHours(-8)
        } -MaxEvents 100 -ErrorAction Stop | Where-Object { $_.Message -match 'CheJinWorkerUpdate|staging\.extracting' } |
            ForEach-Object { @{ id = $_.Id; utc = $_.TimeCreated.ToUniversalTime().ToString('o'); matched_update_cache = $true } })
    } catch { $report.defender_events_unavailable = Failure $_.Exception }
} catch { $report.errors += Failure $_.Exception }
finally {
    if ($owned) {
        try {
            Assert-PlainPath $owned
            foreach ($name in @('staging.extracting','staging')) {
                $dir = Join-Path $owned $name
                if ([IO.Directory]::Exists($dir)) {
                    Assert-PlainPath $dir
                    $file = Join-Path $dir 'synthetic.txt'
                    if ([IO.File]::Exists($file)) { Assert-PlainPath $file; [IO.File]::Delete($file) }
                    [IO.Directory]::Delete($dir, $false)
                }
            }
            [IO.Directory]::Delete($owned, $false)
            $report.synthetic_cleanup = 'removed_only_this_run_known_files_and_directories'
        } catch { $report.synthetic_cleanup_error = Failure $_.Exception }
    }
    $report.finished_utc = [DateTime]::UtcNow.ToString('o')
    [IO.Directory]::CreateDirectory($outputRoot) | Out-Null
    $reportFile = Join-Path $outputRoot 'update-access-report.json'
    [IO.File]::WriteAllText($reportFile, ($report | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
    $zip = $outputRoot + '.zip'
    Compress-Archive -LiteralPath $reportFile -DestinationPath $zip
    Write-Host 'Diagnostic finished. This did NOT repair or upgrade Worker.'
    Write-Host ('Please send the report ZIP next to this tool: ' + [IO.Path]::GetFileName($zip))
    if ($report.elevated) { Write-Host 'Elevated context detected; permission results may differ from the normal Worker.' }
}

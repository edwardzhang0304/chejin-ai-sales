param(
  [string]$Configuration = "",
  [switch]$ConfirmAutoTextDisabled,
  [switch]$CreateShortcuts,
  [switch]$ShowErrorDialog
)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'CheJinWorkerClient.exe'
$report = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + '.json')
$savedEnvironment = @{}
foreach ($name in @('CHEJIN_WORKER_HOME', 'CHEJIN_C2_ENABLED', 'CHEJIN_API_BASE_URL')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
function Show-LaunchFailure([string]$Code) {
    $shell = New-Object -ComObject WScript.Shell
    [void]$shell.Popup(('Worker 启动未完成，请联系管理员检查启动配置和本机日志。' + [Environment]::NewLine + '错误码：' + $Code), 0, '车金 Worker', 16)
}
function Save-UnicodeShortcut([string]$Path, [string]$Target, [string]$WorkingDirectory, [string]$Arguments) {
    # WScript.Shell.Save can convert a Unicode link filename to the system ANSI
    # code page. IShellLinkW + IPersistFile keeps paths Unicode end to end.
    if (-not ('CheJin.ShortcutWriter' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace CheJin {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr id);
        void SetIDList(IntPtr id);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short value);
        void SetHotkey(short value);
        void GetShowCmd(out int value);
        void SetShowCmd(int value);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    public static class ShortcutWriter {
        public static void Save(string path, string target, string directory, string arguments) {
            object instance = new ShellLink();
            try {
                IShellLinkW link = (IShellLinkW)instance;
                link.SetPath(target);
                link.SetWorkingDirectory(directory);
                link.SetArguments(arguments);
                ((IPersistFile)instance).Save(path, true);
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
    }
}
'@
    }
    [CheJin.ShortcutWriter]::Save($Path, $Target, $WorkingDirectory, $Arguments)
}
$errorCode = 'HOOK_LAUNCH_FAILED'
try {
    if (-not (Test-Path -LiteralPath $exe)) { $errorCode = 'HOOK_WORKER_EXECUTABLE_MISSING'; throw $errorCode }
    if (-not $env:LOCALAPPDATA) { $errorCode = 'HOOK_LOCALAPPDATA_MISSING'; throw $errorCode }
    $env:CHEJIN_WORKER_HOME = Join-Path $env:LOCALAPPDATA 'CheJinWorkerHook'
    $env:CHEJIN_C2_ENABLED = 'false'
    $arguments = @('--hook-launch-info', '--write-report', ('"' + $report + '"'))
    if ($Configuration) {
        if (-not $ConfirmAutoTextDisabled) { throw 'Confirm automatic voice transcription is OFF before setup' }
        $resolved = (Resolve-Path -LiteralPath $Configuration).Path
        $arguments = @('--hook-configure', ('"' + $resolved + '"'), '--confirm-auto-text-disabled', '--write-report', ('"' + $report + '"'))
    }
    # Start-Process -Wait includes the updater descendant. Keep this launcher's
    # native current directory outside the program tree while that tree is renamed.
    # Set-Location alone does not change the native process current directory.
    $safeDirectory = [IO.Path]::GetTempPath()
    Set-Location -LiteralPath $safeDirectory
    [Environment]::CurrentDirectory = $safeDirectory
    $check = Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot -ArgumentList $arguments -Wait -PassThru
    if ($check.ExitCode -ne 0) {
        $errorCode = 'HOOK_INSTALL_FAILED'
        if (Test-Path -LiteralPath $report) {
            $failure = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($failure.error_code -match '^HOOK_[A-Z0-9_]+$') { $errorCode = $failure.error_code }
        }
        throw "$errorCode (exit $($check.ExitCode)); no WeChat started"
    }
    $launch = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($CreateShortcuts) {
        $desktop = [Environment]::GetFolderPath('Desktop')
        if (-not $desktop) { throw 'DESKTOP_FOLDER_UNAVAILABLE' }
        New-Item -ItemType Directory -Path $desktop -Force | Out-Null
        Save-UnicodeShortcut -Path (Join-Path $desktop 'Work WeChat.lnk') -Target $launch.exe -WorkingDirectory $launch.working_directory -Arguments ($launch.arguments -join ' ')
        $workerArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -ShowErrorDialog'
        Save-UnicodeShortcut -Path (Join-Path $desktop 'CheJin Worker.lnk') -Target (Join-Path $PSHOME 'powershell.exe') -WorkingDirectory $safeDirectory -Arguments $workerArguments
        Save-UnicodeShortcut -Path (Join-Path $desktop '车金故障诊断.lnk') -Target $exe -WorkingDirectory $safeDirectory -Arguments '--export-diagnostics'
    }
    if ($Configuration -or $CreateShortcuts) {
        Write-Host 'Setup complete. Use the desktop shortcuts. No work has started.'
        exit 0
    }
    $env:CHEJIN_API_BASE_URL = $launch.api_base_url
    if (-not (Get-Process -Name WeChat,Weixin -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $launch.exe -WorkingDirectory $launch.working_directory -ArgumentList $launch.arguments
    }
    Write-Host 'Keep work WeChat open. Worker verifies the existing process; Start accepting remains manual.'
    $worker = Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot -Wait -PassThru
    if ($ShowErrorDialog -and $worker.ExitCode -ne 0) { Show-LaunchFailure ('HOOK_WORKER_EXIT_' + $worker.ExitCode) }
    exit $worker.ExitCode
} catch {
    if (-not $ShowErrorDialog) { throw }
    Show-LaunchFailure $errorCode
    exit 1
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Force }
}

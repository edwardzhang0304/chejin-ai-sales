param(
  [Parameter(Mandatory = $true)]
  [string]$ScriptPath
)

$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -ne 5 -or
    $PSVersionTable.PSVersion.Minor -ne 1 -or
    $PSVersionTable.PSEdition -ne "Desktop") {
  throw "This gate must run with Windows PowerShell 5.1 Desktop."
}

$resolvedPath = (Resolve-Path -LiteralPath $ScriptPath -ErrorAction Stop).Path
$bytes = [System.IO.File]::ReadAllBytes($resolvedPath)
if ($bytes.Length -lt 3 -or
    $bytes[0] -ne 0xEF -or
    $bytes[1] -ne 0xBB -or
    $bytes[2] -ne 0xBF) {
  throw "UAT launcher must start with UTF-8 BOM bytes EF BB BF."
}

$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  $resolvedPath,
  [ref]$tokens,
  [ref]$parseErrors
) | Out-Null

if (@($parseErrors).Count -ne 0) {
  $summary = @(
    $parseErrors | ForEach-Object {
      "$($_.Extent.StartLineNumber):$($_.Extent.StartColumnNumber) $($_.Message)"
    }
  ) -join " | "
  throw "Windows PowerShell 5.1 parse errors: $summary"
}

$record = [ordered]@{
  gate = "POWERSHELL_5_1_PARSE_GATE"
  powershell_version = $PSVersionTable.PSVersion.ToString()
  ps_edition = [string]$PSVersionTable.PSEdition
  script_path = $resolvedPath
  script_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedPath).Hash
  bom_hex = "EFBBBF"
  parse_error_count = @($parseErrors).Count
}
Write-Output ("POWERSHELL_5_1_PARSE_GATE " + ($record | ConvertTo-Json -Compress))

# Execute the production permission gate with synthetic OS facts. No WeChat,
# registry writes, package extraction, user data or shortcuts are touched.
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '../new-pc-setup.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $source), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$gate = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-SetupUserContext' }, $true)
if (-not $gate) { throw 'Production user-context gate is missing.' }
Invoke-Expression $gate.Extent.Text
function Get-SetupUserContext {
    if ($script:collectionFailure) { throw 'Synthetic OS inspection failure' }
    return $script:context
}
function New-Context {
    return [pscustomobject]@{
        UserSid = 'S-1-5-21-100-200-300-1001'; SessionId = 2
        DesktopSids = @('S-1-5-21-100-200-300-1001'); Elevated = $false
        EnableLUA = 1; FilterAdministratorToken = $null
    }
}
$passed = 0
function Check([string]$Name, [bool]$Allowed) {
    $failure = $null
    try { Assert-SetupUserContext } catch { $failure = $_ }
    if ($Allowed -eq ($null -ne $failure)) { throw "Unexpected permission result: $Name / $failure" }
    $script:passed++
    Write-Host "PASS $Name"
}
$context = New-Context
Check 'normal desktop user' $true
$context.Elevated = $true
Check 'manual elevation with UAC enabled' $false
$context.EnableLUA = 0
Check 'same desktop user with UAC disabled' $true
$context.UserSid = 'S-1-5-21-100-200-300-500'; $context.DesktopSids = @($context.UserSid)
Check 'reported built-in Administrator with UAC disabled' $true
$context.EnableLUA = 1
Check 'built-in Administrator with default unfiltered token' $true
$context.FilterAdministratorToken = 0
Check 'built-in Administrator with explicit unfiltered token' $true
$context.FilterAdministratorToken = 1
Check 'manually elevated filtered built-in Administrator' $false
$context.EnableLUA = $null
Check 'unknown UAC policy fails closed when elevated' $false
$context.EnableLUA = 7
Check 'invalid UAC policy fails closed when elevated' $false
$context.EnableLUA = 0; $context.DesktopSids = @('S-1-5-21-100-200-300-1001')
Check 'different desktop user even with UAC disabled' $false
$context = New-Context; $context.DesktopSids = @('S-1-5-21-100-200-300-500')
Check 'different desktop user without elevation' $false
$context.DesktopSids = @()
Check 'desktop not found' $false
$context.DesktopSids = @($context.UserSid, 'S-1-5-21-100-200-300-500')
Check 'ambiguous desktop owners' $false
$context.DesktopSids = @($context.UserSid, $context.UserSid)
Check 'multiple explorer processes belonging to same user' $true
$context.SessionId = 0
Check 'noninteractive session' $false
$context = New-Context; $collectionFailure = $true
Check 'OS inspection failure' $false
$collectionFailure = $false
Write-Host "PASS $passed production permission-gate cases; Windows OS collection and real installation still require native acceptance."

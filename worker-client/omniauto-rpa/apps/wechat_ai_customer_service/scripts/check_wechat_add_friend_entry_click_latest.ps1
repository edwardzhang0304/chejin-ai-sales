param(
    [string]$ProjectRoot = "",
    [ValidateSet("windows")]
    [string]$Route = "windows",
    [string]$ArtifactScope = "",
    [string]$PlanJson = "",
    [string]$ReviewJson = "",
    [string]$ExpectedVerifyMessage = "",
    [string]$ExpectedRemarkName = "",
    [string]$ExpectedRemarkCode = "",
    [string]$ExpectedTaskStatus = "completed",
    [string]$ExpectedResultCode = "invite_sent"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..\..")
}

function Resolve-AddFriendArtifactScope {
    param(
        [string]$RouteName,
        [string]$ExplicitScope
    )
    if (-not [string]::IsNullOrWhiteSpace($ExplicitScope)) {
        return $ExplicitScope
    }
    switch ($RouteName.ToLowerInvariant()) {
        "windows" { return "add_friend_entry_click_plan_windows" }
        default { return "add_friend_entry_click_plan_windows" }
    }
}

$ResolvedArtifactScope = Resolve-AddFriendArtifactScope -RouteName $Route -ExplicitScope $ArtifactScope
$LatestRoot = Join-Path $ProjectRoot ("runtime\{0}\latest" -f $ResolvedArtifactScope)

if ([string]::IsNullOrWhiteSpace($PlanJson)) {
    $PlanJson = Join-Path $LatestRoot "add_friend_entry_click_plan.json"
}
if ([string]::IsNullOrWhiteSpace($ReviewJson)) {
    $ReviewJson = Join-Path $LatestRoot "add_friend_entry_click_review.json"
}

$Failures = New-Object System.Collections.Generic.List[string]

function Add-Failure {
    param([string]$Message)
    [void]$Failures.Add($Message)
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Add-Failure "Missing JSON file: $Path"
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Add-Failure "Invalid JSON file: $Path ($($_.Exception.Message))"
        return $null
    }
}

function Assert-Equals {
    param(
        [string]$Name,
        [object]$Actual,
        [object]$Expected
    )
    if ([string]$Actual -ne [string]$Expected) {
        Add-Failure "$Name expected '$Expected', got '$Actual'"
    }
}

function Assert-NonEmpty {
    param(
        [string]$Name,
        [object]$Actual
    )
    if ([string]::IsNullOrWhiteSpace([string]$Actual)) {
        Add-Failure "$Name is empty"
    }
}

$Plan = Read-JsonFile $PlanJson
$Review = Read-JsonFile $ReviewJson

if ($null -ne $Plan) {
    Assert-Equals "plan.task_status" $Plan.task_status $ExpectedTaskStatus
    Assert-Equals "plan.result_code" $Plan.result_code $ExpectedResultCode
    if (-not [string]::IsNullOrWhiteSpace([string]$Plan.error_code)) {
        Add-Failure "plan.error_code expected empty, got '$($Plan.error_code)'"
    }

    if ([string]::IsNullOrWhiteSpace($ExpectedVerifyMessage)) {
        Assert-NonEmpty "plan.verify_message" $Plan.verify_message
    } else {
        Assert-Equals "plan.verify_message" $Plan.verify_message $ExpectedVerifyMessage
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedRemarkName)) {
        Assert-NonEmpty "plan.remark_name" $Plan.remark_name
    } else {
        Assert-Equals "plan.remark_name" $Plan.remark_name $ExpectedRemarkName
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedRemarkCode)) {
        Assert-NonEmpty "plan.remark_code" $Plan.remark_code
    } else {
        Assert-Equals "plan.remark_code" $Plan.remark_code $ExpectedRemarkCode
    }
    if ($Plan.remark_code_valid -ne $true) {
        Add-Failure "plan.remark_code_valid expected true, got '$($Plan.remark_code_valid)'"
    }

    $NativeEvents = @($Plan.native_diagnostic_events)
    $DiagnosticEvents = @($Plan.diagnostic_events)
    if ($NativeEvents.Count -lt 1) {
        Add-Failure "plan.native_diagnostic_events is empty"
    }
    if ($DiagnosticEvents.Count -lt 1) {
        Add-Failure "plan.diagnostic_events is empty"
    }
}

if ($null -ne $Review) {
    Assert-Equals "review.schema" $Review.schema "add_friend.step_events.v1"
    $Summary = $Review.summary
    if ($null -eq $Summary) {
        Add-Failure "review.summary is missing"
    } else {
        if (-not [string]::IsNullOrWhiteSpace($ExpectedVerifyMessage)) {
            Assert-Equals "review.summary.verify_message" $Summary.verify_message $ExpectedVerifyMessage
        }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedRemarkName)) {
            Assert-Equals "review.summary.remark_name" $Summary.remark_name $ExpectedRemarkName
        }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedRemarkCode)) {
            Assert-Equals "review.summary.remark_code" $Summary.remark_code $ExpectedRemarkCode
        }
        if ($Summary.remark_code_valid -ne $true) {
            Add-Failure "review.summary.remark_code_valid expected true, got '$($Summary.remark_code_valid)'"
        }
    }

    $Events = @($Review.events)
    if ($Events.Count -lt 1) {
        Add-Failure "review.events is empty"
    }
    $ExpectedTerminalStepId = ""
    switch ($ExpectedResultCode) {
        "invite_sent" { $ExpectedTerminalStepId = "invite_confirm_after_click" }
        "already_friend" { $ExpectedTerminalStepId = "add_contact_search_terminal" }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedTerminalStepId)) {
        $TerminalEvents = @($Events | Where-Object { $_.step_id -eq $ExpectedTerminalStepId })
        if ($TerminalEvents.Count -lt 1) {
            Add-Failure "review.events missing $ExpectedTerminalStepId"
        }
    }
}

foreach ($Path in @($PlanJson, $ReviewJson)) {
    if (Test-Path -LiteralPath $Path) {
        $Text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if ($ExpectedResultCode -ne "already_friend" -and $Text -match '"already_friend"') {
            Add-Failure "$Path contains already_friend"
        }
    }
}

if ($Failures.Count -gt 0) {
    Write-Host "add_friend latest report check: FAILED"
    foreach ($Failure in $Failures) {
        Write-Host " - $Failure"
    }
    exit 1
}

Write-Host "add_friend latest report check: OK"
Write-Host "Route: $Route"
Write-Host "ArtifactScope: $ResolvedArtifactScope"
Write-Host "PlanJson: $PlanJson"
Write-Host "ReviewJson: $ReviewJson"
exit 0

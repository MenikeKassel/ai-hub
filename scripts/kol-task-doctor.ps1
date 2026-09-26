[CmdletBinding()]
param([string]$RepoRoot = "")

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$contractPath = Join-Path $PSScriptRoot "kol-task-contract.json"
$contract = Get-Content -LiteralPath $contractPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($contract.owner -ne "install-kol-recovery-tasks.ps1") { throw "Unexpected KOL task owner in $contractPath" }

$checks = foreach ($spec in $contract.tasks) {
    $scriptPath = Join-Path $PSScriptRoot ([string]$spec.script)
    $expectedArguments = (@(
        "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", "`"$scriptPath`"", "-RepoRoot", "`"$RepoRoot`""
    ) + @($spec.arguments)) -join " "
    $task = Get-ScheduledTask -TaskName ([string]$spec.name) -ErrorAction SilentlyContinue
    $actualAt = ""
    $actualArguments = ""
    $reasons = @()
    if ($null -eq $task) {
        $reasons += "missing"
    } else {
        if (@($task.Triggers).Count -ne 1) {
            $reasons += "trigger_count"
        } else {
            try {
                $actualAt = [DateTimeOffset]::Parse([string]$task.Triggers[0].StartBoundary).ToString("HH:mm")
            } catch {
                $reasons += "trigger_time_unreadable"
            }
            if ($actualAt -and $actualAt -ne [string]$spec.at) { $reasons += "trigger_time" }
        }
        if (@($task.Actions).Count -ne 1) {
            $reasons += "action_count"
        } else {
            $actualArguments = [string]$task.Actions[0].Arguments
            $normalizedActual = $actualArguments -replace '"([^"\s]+)"', '$1'
            $normalizedExpected = $expectedArguments -replace '"([^"\s]+)"', '$1'
            if ([string]$task.Actions[0].Execute -ine "powershell.exe" -or $normalizedActual -ine $normalizedExpected) {
                $reasons += "action"
            }
        }
        if ([string]$task.Principal.RunLevel -notin @("Highest", "1")) { $reasons += "run_level" }
    }
    [pscustomobject]@{
        name = [string]$spec.name
        status = if ($reasons.Count) { "drift" } else { "ok" }
        reasons = $reasons
        expected_at = [string]$spec.at
        actual_at = $actualAt
        expected_arguments = $expectedArguments
        actual_arguments = $actualArguments
    }
}

$marketTask = Get-ScheduledTask -TaskName "Market_Data_Sync_Daily" -ErrorAction SilentlyContinue
$marketTimes = @()
if ($null -ne $marketTask) {
    $marketTimes = @($marketTask.Triggers | ForEach-Object {
        try { [DateTimeOffset]::Parse([string]$_.StartBoundary).ToString("HH:mm") } catch { "" }
    })
}
$marketReasons = @()
if ($null -eq $marketTask) { $marketReasons += "missing" }
elseif ($marketTimes -notcontains [string]$contract.market_publication_at) { $marketReasons += "trigger_time" }
$checks += [pscustomobject]@{
    name = "Market_Data_Sync_Daily"
    status = if ($marketReasons.Count) { "drift" } else { "ok" }
    reasons = $marketReasons
    expected_at = [string]$contract.market_publication_at
    actual_at = $marketTimes -join ","
    expected_arguments = ""
    actual_arguments = ""
}

$warnings = @()
$xPolicy = [pscustomobject]@{
    status = "unavailable"
    reference = $contract.x_policy_reference
    actual = $null
    drift_fields = @()
}
try {
    $runtimePolicy = Invoke-RestMethod -Uri "http://127.0.0.1:8123/api/system/x-sessions" -TimeoutSec 3
    $actualPolicy = [pscustomobject]@{
        global_limit_24h = [int]$runtimePolicy.global_limit_24h
        session_limit_24h = [int]$runtimePolicy.session_limit_24h
        min_interval_seconds = [int]$runtimePolicy.min_interval_seconds
    }
    $driftFields = @("global_limit_24h", "session_limit_24h", "min_interval_seconds" | Where-Object {
        $actualPolicy.$_ -ne $contract.x_policy_reference.$_
    })
    $xPolicy = [pscustomobject]@{
        status = if ($driftFields.Count) { "override" } else { "reference" }
        reference = $contract.x_policy_reference
        actual = $actualPolicy
        drift_fields = $driftFields
    }
    if ($driftFields.Count) {
        $warnings += [pscustomobject]@{
            code = "x_policy_reference_drift"
            message = "X runtime budget differs from the documented reference; operator override is preserved."
            fields = $driftFields
        }
    }
} catch {
    $warnings += [pscustomobject]@{
        code = "x_policy_unavailable"
        message = "X runtime policy could not be read from the local API."
        fields = @()
    }
}

$payload = [pscustomobject]@{
    ok = @($checks | Where-Object { $_.status -ne "ok" }).Count -eq 0
    owner = [string]$contract.owner
    contract_version = [int]$contract.version
    checks = @($checks)
    x_policy = $xPolicy
    warnings = @($warnings)
}
$payload | ConvertTo-Json -Depth 6
if (-not $payload.ok) { exit 2 }

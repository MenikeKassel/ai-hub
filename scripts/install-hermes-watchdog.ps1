[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$TaskName = "Hermes_Gateway_Watchdog",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$watchdog = Join-Path $RepoRoot "scripts\hermes-watchdog.ps1"
$launcher = Join-Path $RepoRoot "scripts\hermes-watchdog-launcher.vbs"
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Hermes watchdog script not found: $watchdog"
}
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Hermes watchdog launcher not found: $launcher"
}

$taskAction = "wscript.exe `"$launcher`""
$arguments = @(
    "/Create",
    "/TN", $TaskName,
    "/TR", $taskAction,
    "/SC", "MINUTE",
    "/MO", [string]$IntervalMinutes,
    "/RL", "LIMITED",
    "/F"
)

& schtasks.exe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Unable to create Windows Scheduled Task: $TaskName"
}

if ($RunNow) {
    & schtasks.exe /Run /TN $TaskName
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to run Windows Scheduled Task: $TaskName"
    }
}

Write-Host "Installed Hermes watchdog task: $TaskName (every $IntervalMinutes minutes)"

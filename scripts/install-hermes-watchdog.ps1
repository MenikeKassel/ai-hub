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
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Hermes watchdog script not found: $watchdog"
}

$taskAction = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`""
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

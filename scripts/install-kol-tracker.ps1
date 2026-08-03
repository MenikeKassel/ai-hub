[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$TaskName = "KOL_Return_Tracker_Daily",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$venv = Join-Path $RepoRoot "_runtime\venv-trading"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $RepoRoot "_automation\trading_research\requirements.txt"
$runner = Join-Path $RepoRoot "scripts\kol-tracker.ps1"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"

foreach ($requiredPath in @($requirements, $runner, $cli)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required file not found: $requiredPath"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv $venv
    } else {
        & python -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the trading virtual environment."
    }
}

& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install trading dependencies."
}

& $python $cli kol-init
if ($LASTEXITCODE -ne 0) {
    throw "Unable to initialize the KOL event store."
}

$actionArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -RepoRoot `"$RepoRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "20:00"
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -Hidden
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Update audited KOL recommendation returns and the local report." `
    -Force | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Installed scheduled task: $TaskName (Monday-Friday at 20:00)"
Write-Host "Python: $python"
Write-Host "Runner: $runner"

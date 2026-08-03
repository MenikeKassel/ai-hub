[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$runner = Join-Path $RepoRoot "scripts\event-method-research.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Event research runner not found: $runner"
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -RepoRoot `"$RepoRoot`""
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -Hidden

Register-ScheduledTask `
    -TaskName "KOL_Event_Method_Research" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Complete point-in-time multi-method evidence and DeepSeek interpretation for pending formal KOL events." `
    -Force | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName "KOL_Event_Method_Research"
}

Write-Host "Installed KOL_Event_Method_Research with a 02:00 fallback; the daily board task triggers it on completion."

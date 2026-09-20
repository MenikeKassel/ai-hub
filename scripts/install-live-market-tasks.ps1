[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$limited = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) -Hidden

function Register-LiveTask {
    param([string]$Name,[string]$Script,[string]$ExtraArguments,[object]$Trigger,[string]$Description,[object]$TaskSettings=$settings)
    $scriptPath = Join-Path $RepoRoot "scripts\$Script"
    if (-not (Test-Path -LiteralPath $scriptPath)) { throw "Task script not found: $scriptPath" }
    $arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" -RepoRoot `"$RepoRoot`" $ExtraArguments".Trim()
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $TaskSettings -Principal $limited -Description $Description -Force | Out-Null
}

$remove = @(
    "FreeStockDB_Update_Daily", "Market_Data_Weekly", "KOL_Return_Tracker_Daily", "KOL_Surge_Alert_Daily",
    "KOL_Event_Method_Research", "KOL_Performance_Weekly", "Research_Data_Digest_Daily"
)
foreach ($name in $remove) {
    try { if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop } } catch { Write-Warning "Could not remove ${name}: $($_.Exception.Message)" }
}

$freeSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -Hidden
Register-LiveTask "FreeStockDB_Update_Daily" "freestockdb-update.ps1" "-DataRoot `"$(Join-Path (Split-Path -Parent $RepoRoot) 'freestock\stockdb')`"" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "17:50") "Verify and update the local FreeStockDB source before the daily market publication." $freeSettings
Register-LiveTask "Market_Data_Sync_Daily" "market-data-sync.ps1" "" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "19:30") "Publish the latest completed daily market snapshot atomically." $settings
Register-LiveTask "KOL_Return_Tracker_Daily" "kol-tracker.ps1" "" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "23:30") "Fallback update for audited KOL event returns; successful market publication triggers it immediately." $settings
Register-LiveTask "KOL_Surge_Alert_Daily" "kol-surge-alert.ps1" "" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "23:40") "Push posts whose mentioned stocks gained 10 percent or more within a week of the post." $settings

if ($RunNow) { Start-ScheduledTask -TaskName "Market_Data_Sync_Daily" }
Write-Host "Installed live tasks: FreeStockDB 17:50, atomic market publication 19:30, and KOL returns after publication (23:30 fallback). Event research remains manual."

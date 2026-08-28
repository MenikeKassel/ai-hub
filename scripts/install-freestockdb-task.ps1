[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunNow,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$script = Join-Path $RepoRoot "scripts\freestockdb-update.ps1"
$taskName = "FreeStockDB_Update_Daily"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"$(if ($DryRun) { ' -DryRun' } else { '' })"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 17:50
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Verify and update the isolated local FreeStockDB mirror before daily market sync." -Force | Out-Null
Write-Output "Installed $taskName at 17:50 on weekdays."
if ($RunNow) { Start-ScheduledTask -TaskName $taskName }
$installed = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "Verified $($installed.TaskName): state=$($installed.State), next=$($info.NextRunTime), last_result=$($info.LastTaskResult)"

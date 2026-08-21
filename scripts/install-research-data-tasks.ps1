[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$requirements = Join-Path $RepoRoot "_automation\trading_research\requirements.txt"
$ocrInstaller = Join-Path $RepoRoot "scripts\install-fast-ocr.ps1"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }

& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Unable to install trading dependencies." }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ocrInstaller -RepoRoot $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Unable to install the RapidOCR runtime." }
& $python $cli kol-post-db-backup
if ($LASTEXITCODE -ne 0) { throw "Unable to back up the KOL post database before task installation." }
& $python $cli market-init
if ($LASTEXITCODE -ne 0) { throw "Unable to initialize market data storage." }

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

function Register-ResearchTask([string]$Name, [string]$ScriptName, $Trigger, [string]$Description, $TaskSettings = $null, [string]$ExtraArguments = "") {
    $script = Join-Path $RepoRoot "scripts\$ScriptName"
    if (-not (Test-Path -LiteralPath $script)) { throw "Task script not found: $script" }
    $arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -RepoRoot `"$RepoRoot`" $ExtraArguments".Trim()
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $effectiveSettings = if ($null -ne $TaskSettings) { $TaskSettings } else { $settings }
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $effectiveSettings -Principal $principal -Description $Description -Force | Out-Null
}

Register-ResearchTask "Market_Data_Sync_Daily" "market-data-sync.ps1" @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "19:30"),
    (New-ScheduledTaskTrigger -Daily -At "23:30"),
    (New-ScheduledTaskTrigger -Daily -At "06:30")
) "Read the current unified A-share foundation release and refresh derived market data only when the release changes."
Register-ResearchTask "FreeStockDB_Update_Daily" "freestockdb-update.ps1" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "17:50") "Verify and update the isolated local FreeStockDB mirror before market sync." (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -Hidden)
Register-ResearchTask "KOL_Zhihu_Fetch_Morning" "kol-post-fetch.ps1" (New-ScheduledTaskTrigger -Daily -At "06:30") "Fetch active Zhihu KOL answers before the morning review queue." $settings "-Platform zhihu -FetchCount 10 -NoNotify -SkipAiPrefill"
Register-ResearchTask "KOL_Zhihu_Fetch_Evening" "kol-post-fetch.ps1" (New-ScheduledTaskTrigger -Daily -At "19:20") "Fetch active Zhihu KOL answers for the next morning preview." $settings "-Platform zhihu -FetchCount 10 -NoNotify"
$morningSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 55) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$morningTriggers = @(
    (New-ScheduledTaskTrigger -Daily -At "07:20"),
    (New-ScheduledTaskTrigger -Daily -At "08:05"),
    (New-ScheduledTaskTrigger -Daily -At "08:45")
)
foreach ($legacyTask in "KOL_Morning_Initial", "KOL_Morning_Refresh") {
    if (Get-ScheduledTask -TaskName $legacyTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $legacyTask -Confirm:$false
    }
}
Register-ResearchTask "KOL_Morning_Pipeline" "kol-morning-pipeline.ps1" $morningTriggers "Prepare and finalize the evidence-first KOL morning review queue before 09:00." $morningSettings "-Platform x"
Register-ResearchTask "Research_Data_Digest_Daily" "research-data-digest.ps1" (New-ScheduledTaskTrigger -Daily -At "02:30") "Send one combined KOL, review-agent, and market data digest."
Register-ResearchTask "Market_Data_Weekly" "market-weekly-refresh.ps1" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "10:00") "Refresh instrument master, financial, announcement, and vendor-labelled fund-flow snapshots."
Register-ResearchTask "KOL_Event_Method_Research" "event-method-research.ps1" (New-ScheduledTaskTrigger -Daily -At "02:00") "Complete point-in-time multi-method evidence and AI interpretation." (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3) -Hidden)
Register-ResearchTask "KOL_Performance_Weekly" "kol-performance-weekly.ps1" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "08:50") "Send the weekly batch-weighted KOL performance summary after mature checkpoints settle." (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -Hidden)

if (Get-ScheduledTask -TaskName "KOL_Post_Classify_Daily" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "KOL_Post_Classify_Daily" -Confirm:$false
}
if (Get-ScheduledTask -TaskName "KOL_Review_Agent" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "KOL_Review_Agent" -Confirm:$false
}

if ($RunNow) { Start-ScheduledTask -TaskName "Market_Data_Sync_Daily" }
Write-Host "Installed Zhihu fetch, morning orchestration, market sync, event research, digest, and weekly market tasks."

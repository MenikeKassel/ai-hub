[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunUiNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$isAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -RepoRoot `"$RepoRoot`""
    if ($RunUiNow) { $arguments += " -RunUiNow" }
    $elevated = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    exit $elevated.ExitCode
}

function Register-KolTask {
    param(
        [string]$Name,
        [string]$Script,
        [string[]]$Arguments = @(),
        [object]$Trigger,
        [string]$Description,
        [TimeSpan]$Limit = (New-TimeSpan -Hours 2)
    )
    $args = @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", "`"$Script`"") + $Arguments
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($args -join " ") -WorkingDirectory $RepoRoot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit $Limit -Hidden
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Principal $principal -Description $Description -Force | Out-Null
}

$ui = Join-Path $RepoRoot "scripts\start-kol-ui.ps1"
$freestock = Join-Path $RepoRoot "scripts\start-freestockdb.ps1"
$fetch = Join-Path $RepoRoot "scripts\kol-post-fetch.ps1"
$morning = Join-Path $RepoRoot "scripts\kol-morning-pipeline.ps1"
$nitter = Join-Path $RepoRoot "scripts\start-kol-nitter.ps1"
$watchdog = Join-Path $RepoRoot "scripts\hermes-watchdog.ps1"
foreach ($path in @($ui, $freestock, $fetch, $morning, $nitter, $watchdog)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required script not found: $path" }
}

$disabledTasks = @(
    "FreeStockDB_Update_Daily",
    "Market_Data_Sync_Daily",
    "Market_Data_Weekly",
    "KOL_Return_Tracker_Daily",
    "KOL_Review_Agent",
    "KOL_Event_Method_Research",
    "KOL_Performance_Weekly",
    "Research_Data_Digest_Daily",
    "KOL_Morning_Initial",
    "KOL_Morning_Refresh",
    "KOL_Post_Fetch_Zhihu_Morning",
    "KOL_Post_Fetch_Zhihu_Evening"
)
foreach ($task in $disabledTasks) {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
    }
}

$atLogon = New-ScheduledTaskTrigger -AtLogOn
$workspaceRoot = Split-Path -Parent $RepoRoot
$freeStockRoot = Join-Path $workspaceRoot "freestock\stockdb"
Register-KolTask -Name "KOL_FreeStockDB_Start" -Script $freestock -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-FreeStockRoot", "`"$freeStockRoot`"") -Trigger $atLogon -Description "Start the local read-only FreeStockDB service before the KOL console." -Limit (New-TimeSpan -Minutes 5)
Register-KolTask -Name "KOL_UI_Start" -Script $ui -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-NoBrowser") -Trigger $atLogon -Description "Start the local KOL research console on logon." -Limit (New-TimeSpan -Hours 1)
Register-KolTask -Name "KOL_Nitter_Shadow_Logon" -Script $nitter -Arguments @("-RepoRoot", "`"$RepoRoot`"") -Trigger $atLogon -Description "Start Nitter as an optional shadow fallback; never the primary collector." -Limit (New-TimeSpan -Hours 2)

Register-KolTask -Name "KOL_Post_Fetch_Daily" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "x", "-FetchCount", "50", "-SkipAiPrefill") -Trigger (New-ScheduledTaskTrigger -Daily -At "19:00") -Description "Collect watched X KOL posts with isolated reader credentials."
# Keep one canonical scheduled task per Zhihu time window. The API starts
# these stable names directly, so a second alias would run the fetch twice.
Register-KolTask -Name "KOL_Zhihu_Fetch_Morning" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "zhihu", "-FetchCount", "50", "-SkipAiPrefill") -Trigger (New-ScheduledTaskTrigger -Daily -At "06:30") -Description "Operator entrypoint for the morning Zhihu fetch."
Register-KolTask -Name "KOL_Zhihu_Fetch_Evening" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "zhihu", "-FetchCount", "50", "-SkipAiPrefill") -Trigger (New-ScheduledTaskTrigger -Daily -At "19:20") -Description "Operator entrypoint for the evening Zhihu fetch."

Register-KolTask -Name "KOL_Post_Fetch_Manual_X" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "x", "-FetchCount", "50", "-SkipAiPrefill", "-NoNotify") -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)) -Description "Manual X collection entrypoint."
Register-KolTask -Name "KOL_Post_Fetch_Manual_Zhihu" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "zhihu", "-FetchCount", "50", "-SkipAiPrefill", "-NoNotify") -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)) -Description "Manual Zhihu collection entrypoint."

$morningTimes = @(@("KOL_Morning_Pipeline_0720", "07:20", "x"), @("KOL_Morning_Pipeline_0805", "08:05", "zhihu"), @("KOL_Morning_Pipeline_0845", "08:45", "all"))
foreach ($item in $morningTimes) {
    $morningArguments = @("-RepoRoot", "`"$RepoRoot`"", "-Platform", $item[2], "-FetchCount", "20")
    if ($item[0] -eq "KOL_Morning_Pipeline_0845") { $morningArguments += "-NoFetch" }
    Register-KolTask -Name $item[0] -Script $morning -Arguments $morningArguments -Trigger (New-ScheduledTaskTrigger -Daily -At $item[1]) -Description "Run the KOL morning review pipeline in historical market mode."
}
$manualTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)
Register-KolTask -Name "KOL_Morning_Pipeline" -Script $morning -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "all", "-FetchCount", "20") -Trigger $manualTrigger -Description "Manual operator entrypoint for the KOL morning review pipeline in historical market mode."

$classifier = Join-Path $RepoRoot "scripts\kol-post-classify.ps1"
if (-not (Test-Path -LiteralPath $classifier)) { throw "Required script not found: $classifier" }
Register-KolTask -Name "KOL_Post_Classify_Daily" -Script $classifier -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-DailyLimit", "250", "-OcrLimit", "0") -Trigger (New-ScheduledTaskTrigger -Daily -At "09:15") -Description "Drain the candidate OCR backlog without a local daily cap and the bounded model queue." -Limit (New-TimeSpan -Hours 3)

$hermes = Get-Command hermes.exe -ErrorAction SilentlyContinue
if ($hermes) {
    $gatewayAction = New-ScheduledTaskAction -Execute $hermes.Source -Argument "gateway start --quiet" -WorkingDirectory $RepoRoot
    $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Hidden
    Register-ScheduledTask -TaskName "Hermes_Gateway_Logon" -Action $gatewayAction -Trigger $atLogon -Settings $settings -Principal $principal -Description "Start Hermes gateway on user logon." -Force | Out-Null
    Register-KolTask -Name "Hermes_Gateway_Watchdog" -Script $watchdog -Arguments @("-LogPath", "`"$RepoRoot\_runtime\watchdog\hermes-watchdog.log`"") -Trigger $watchTrigger -Description "Check and recover the Hermes gateway every five minutes." -Limit (New-TimeSpan -Minutes 5)
}

if ($RunUiNow) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ui -RepoRoot $RepoRoot -NoBrowser }
Write-Host "Installed KOL collection/UI/Hermes tasks under $RepoRoot. Market sync, return tracker, and research refresh tasks were intentionally not registered."

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$RunUiNow,
    [switch]$CollectionOnly
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$isAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -RepoRoot `"$RepoRoot`""
    if ($RunUiNow) { $arguments += " -RunUiNow" }
    if ($CollectionOnly) { $arguments += " -CollectionOnly" }
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
$watchdogLauncher = Join-Path $RepoRoot "scripts\hermes-watchdog-launcher.vbs"
foreach ($path in @($ui, $freestock, $fetch, $morning, $nitter, $watchdog, $watchdogLauncher)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required script not found: $path" }
}

if (-not $CollectionOnly) {
    $disabledTasks = @(
        "KOL_Review_Agent",
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
}

$taskContractPath = Join-Path $PSScriptRoot "kol-task-contract.json"
$taskContract = Get-Content -LiteralPath $taskContractPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskContract.owner -ne "install-kol-recovery-tasks.ps1") { throw "Unexpected KOL task owner in $taskContractPath" }
foreach ($spec in $taskContract.tasks) {
    $taskScript = Join-Path $PSScriptRoot ([string]$spec.script)
    if (-not (Test-Path -LiteralPath $taskScript)) { throw "Required script not found: $taskScript" }
    $taskArguments = @("-RepoRoot", "`"$RepoRoot`"") + @($spec.arguments)
    Register-KolTask -Name ([string]$spec.name) -Script $taskScript -Arguments $taskArguments -Trigger (New-ScheduledTaskTrigger -Daily -At ([string]$spec.at)) -Description ([string]$spec.description)
}

Register-KolTask -Name "KOL_Post_Fetch_Manual_X" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "x", "-FetchCount", "50", "-SkipAiPrefill", "-NoNotify", "-NotifyOnCompletion") -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)) -Description "Manual X collection entrypoint."
Register-KolTask -Name "KOL_Post_Fetch_Manual_Zhihu" -Script $fetch -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "zhihu", "-FetchCount", "50", "-SkipAiPrefill", "-NoNotify", "-NotifyOnCompletion") -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)) -Description "Manual Zhihu collection entrypoint."

$manualTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10)
Register-KolTask -Name "KOL_Morning_Pipeline" -Script $morning -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-Platform", "all", "-FetchCount", "20") -Trigger $manualTrigger -Description "Manual operator entrypoint for the KOL morning review pipeline in historical market mode."

if ($CollectionOnly) {
    Write-Host "Installed canonical KOL collection and morning tasks from $taskContractPath."
    exit 0
}

$classifier = Join-Path $RepoRoot "scripts\kol-post-classify.ps1"
if (-not (Test-Path -LiteralPath $classifier)) { throw "Required script not found: $classifier" }
Register-KolTask -Name "KOL_Post_Classify_Daily" -Script $classifier -Arguments @("-RepoRoot", "`"$RepoRoot`"", "-DailyLimit", "250", "-OcrLimit", "150") -Trigger (New-ScheduledTaskTrigger -Daily -At "09:15") -Description "Drain the candidate OCR/AI backlog with persistent daily caps." -Limit (New-TimeSpan -Hours 3)

$hermes = Get-Command hermes.exe -ErrorAction SilentlyContinue
if ($hermes) {
    $gatewayAction = New-ScheduledTaskAction -Execute $hermes.Source -Argument "gateway start --quiet" -WorkingDirectory $RepoRoot
    $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Hidden
    Register-ScheduledTask -TaskName "Hermes_Gateway_Logon" -Action $gatewayAction -Trigger $atLogon -Settings $settings -Principal $principal -Description "Start Hermes gateway on user logon." -Force | Out-Null
    $watchdogAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$watchdogLauncher`"" -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName "Hermes_Gateway_Watchdog" -Action $watchdogAction -Trigger $watchTrigger -Settings $settings -Principal $principal -Description "Check and recover the Hermes gateway every five minutes without opening a console window." -Force | Out-Null
}

if ($RunUiNow) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ui -RepoRoot $RepoRoot -NoBrowser }
Write-Host "Installed KOL collection/UI/Hermes tasks under $RepoRoot. Market sync, return tracker, and research refresh tasks were intentionally not registered."

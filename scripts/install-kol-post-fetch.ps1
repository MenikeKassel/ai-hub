[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$TaskName = "KOL_Post_Fetch_Daily",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$venv = Join-Path $RepoRoot "_runtime\venv-trading"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $RepoRoot "_automation\trading_research\requirements.txt"
$ui = Join-Path $RepoRoot "_automation\trading_research\ui"
$runner = Join-Path $RepoRoot "scripts\kol-post-fetch.ps1"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$xtfInstaller = Join-Path $RepoRoot "scripts\install-x-tweet-fetcher.ps1"

foreach ($required in @($requirements, $ui, $runner, $cli, $xtfInstaller)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required" }
}

if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { & $launcher.Source -3 -m venv $venv } else { & python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the trading virtual environment." }
}

& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Unable to install Python dependencies." }
Push-Location $ui
try {
    & npm install
    if ($LASTEXITCODE -ne 0) { throw "Unable to install UI dependencies." }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "Unable to build the KOL UI." }
} finally { Pop-Location }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $xtfInstaller -RepoRoot $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "Unable to install x-tweet-fetcher." }

& $python $cli kol-post-doctor
if ($LASTEXITCODE -ne 0) { throw "KOL post doctor failed." }

$actionArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -RepoRoot `"$RepoRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -Daily -At "19:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -Hidden
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Fetch watched X KOL posts and prepare the review queue." -Force | Out-Null

$manualSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -Hidden
foreach ($manual in @(
    @{ Name = "KOL_Post_Fetch_Manual_X"; Platform = "x" },
    @{ Name = "KOL_Post_Fetch_Manual_Zhihu"; Platform = "zhihu" }
)) {
    $manualArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -RepoRoot `"$RepoRoot`" -Platform $($manual.Platform) -SkipAiPrefill -NoNotify -NotifyOnCompletion"
    $manualAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $manualArguments
    Register-ScheduledTask -TaskName $manual.Name -Action $manualAction -Settings $manualSettings -Principal $principal -Description "Hermes-triggered $($manual.Platform) KOL post collection without AI review." -Force | Out-Null
}

if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Write-Host "Installed scheduled task: $TaskName (daily at 19:00)"
Write-Host "Installed manual tasks: KOL_Post_Fetch_Manual_X, KOL_Post_Fetch_Manual_Zhihu"

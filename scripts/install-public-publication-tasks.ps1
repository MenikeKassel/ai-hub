param(
    [string]$AiHubHome = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$DataRepo = $(if ($env:PUBLIC_DATA_REPO) { $env:PUBLIC_DATA_REPO } else { Join-Path (Split-Path $AiHubHome) 'kol-audit-dataset' }),
    [string]$PublicRepo = $(if ($env:PUBLIC_SYSTEM_REPO) { $env:PUBLIC_SYSTEM_REPO } else { Join-Path (Split-Path $AiHubHome) 'ai-hub-public' }),
    [string]$GitProxy = $env:AI_HUB_GIT_PROXY
)

$ErrorActionPreference = 'Stop'
function Register-PublicTask([string]$Name, [string]$Script, [string]$Arguments, [Microsoft.Management.Infrastructure.CimInstance]$Trigger) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" $Arguments"
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Description 'Publishes sanitized KOL audit artifacts; never uploads private runtime data.' -Force | Out-Null
}
$dataScript = Join-Path $AiHubHome 'scripts\publish-public-data.ps1'
$systemScript = Join-Path $AiHubHome 'scripts\publish-public-system.ps1'
$ghCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
$ghExe = if ($ghCommand) { $ghCommand.Source } else { '' }
if (-not $ghExe) { throw 'gh.exe was not found; install GitHub CLI before installing the mirror task.' }
$pythonExe = Join-Path $AiHubHome '_runtime\venv-trading\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    $pythonExe = if ($pythonCommand) { $pythonCommand.Source } else { '' }
}
if (-not $pythonExe) { throw 'Python interpreter was not found; install the trading runtime before installing publication tasks.' }
Register-PublicTask 'KOL_Public_Dataset_Daily' $dataScript "-SourceRepo `"$AiHubHome`" -DataRepo `"$DataRepo`" -GitProxy `"$GitProxy`" -PythonExe `"$pythonExe`"" (New-ScheduledTaskTrigger -Daily -At 5:30AM)
$release = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 6:00AM
Register-PublicTask 'KOL_Public_Dataset_Weekly_Release' $dataScript "-SourceRepo `"$AiHubHome`" -DataRepo `"$DataRepo`" -GitProxy `"$GitProxy`" -GhExe `"$ghExe`" -PythonExe `"$pythonExe`" -CreateRelease" $release
$watch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(10) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-PublicTask 'AI_Hub_Public_Mirror_Watch' $systemScript "-SourceRepo `"$AiHubHome`" -PublicRepo `"$PublicRepo`" -GitProxy `"$GitProxy`" -GhExe `"$ghExe`" -PythonExe `"$pythonExe`" -CreatePullRequest" $watch
Write-Output (@{ status = 'installed'; tasks = @('KOL_Public_Dataset_Daily', 'KOL_Public_Dataset_Weekly_Release', 'AI_Hub_Public_Mirror_Watch') } | ConvertTo-Json)

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$TaskName = "KOL_Post_Fetch_Daily",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
if ($TaskName -ne "KOL_Post_Fetch_Daily") { throw "Custom KOL fetch task names are unsupported; use the canonical collection installer." }

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

$canonicalInstaller = Join-Path $RepoRoot "scripts\install-kol-recovery-tasks.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $canonicalInstaller -RepoRoot $RepoRoot -CollectionOnly
if ($LASTEXITCODE -ne 0) { throw "Unable to install canonical KOL collection tasks." }

if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Write-Host "Installed canonical KOL collection tasks through install-kol-recovery-tasks.ps1."

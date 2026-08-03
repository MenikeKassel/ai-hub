[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = (Get-Date -Format "yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs"
$mutex = New-Object System.Threading.Mutex($false, "Local\MarketDataWeekly")
$hasLock = $false

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { exit 0 }
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $output = & $python $cli market-weekly --as-of $AsOf 2>&1 | Out-String
    Add-Content -LiteralPath (Join-Path $logDirectory "market-weekly.log") -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $($output.Trim())" -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { exit 1 }
    exit 0
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

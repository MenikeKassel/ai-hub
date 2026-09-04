[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs"
$logPath = Join-Path $logDirectory "market-daily-publish.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\MarketDataSyncDaily")
$hasLock = $false

function Write-MarketLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-MarketLog "Skipped: another market sync holds the mutex."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $marketProxy = if ($env:KOL_MARKET_PROXY) {
        $env:KOL_MARKET_PROXY
    } elseif ($env:KOL_X_PROXY) {
        $env:KOL_X_PROXY
    } else {
        "http://127.0.0.1:7897"
    }
    if ($marketProxy) {
        $env:HTTP_PROXY = $marketProxy
        $env:HTTPS_PROXY = $marketProxy
        $env:NO_PROXY = "127.0.0.1,localhost"
    }
    $report = Join-Path $RepoRoot "_runtime\trading\restore-reports\market-daily-publish-task.json"
    $arguments = @($cli, "market-daily-publish", "--as-of", $(if ($AsOf) { $AsOf } else { "auto" }), "--apply", "--report", $report)
    $stdoutPath = Join-Path $logDirectory (".market-sync-" + [guid]::NewGuid().ToString("N") + ".out")
    $stderrPath = Join-Path $logDirectory (".market-sync-" + [guid]::NewGuid().ToString("N") + ".err")
    try {
        & $python @arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        if (Test-Path -LiteralPath $stdoutPath) { $stdout = Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { $stdout = "" }
        if (Test-Path -LiteralPath $stderrPath) { $stderr = Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { $stderr = "" }
        $stdout = [string]$stdout
        $stderr = [string]$stderr
        if (-not [string]::IsNullOrWhiteSpace($stdout)) { Write-MarketLog $stdout.Trim() }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) { Write-MarketLog ("stderr: " + $stderr.Trim()) }
    } finally {
        Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    }
    if ($exitCode -ne 0) { throw "market-daily-publish exited with code $exitCode" }
    $startUi = Join-Path $RepoRoot "scripts\start-kol-ui.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startUi -RepoRoot $RepoRoot -Port 8123 -NoBrowser
    if ($LASTEXITCODE -ne 0) { throw "KOL UI restart failed after market publication" }
    exit 0
} catch {
    Write-MarketLog "Market sync error: $($_.Exception.Message)"
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

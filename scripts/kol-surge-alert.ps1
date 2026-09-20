[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$LookbackDays = 21,
    [int]$WindowDays = 7,
    [double]$Threshold = 0.10,
    [switch]$NoNotify
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$runtime = Join-Path $RepoRoot "_runtime\trading"
$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$env:PYTHONPATH = ""
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONSTARTUP -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONUSERBASE -ErrorAction SilentlyContinue
$logDirectory = Join-Path $runtime "kol\logs"
$logPath = Join-Path $logDirectory "kol-surge-alert.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLSurgeAlertDaily")
$hasLock = $false
$marketMutex = New-Object System.Threading.Mutex($false, "Local\MarketDataSyncDaily")
$hasMarketLock = $false

function Write-SurgeLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding UTF8
}

function Send-FailureNotification([string]$Message) {
    try {
        Send-HermesUtf8Message -Message "[KOL研究台] 涨幅提醒任务失败：$Message" | Out-Null
    } catch {
        Write-SurgeLog "Hermes failure notification could not be sent: $($_.Exception.Message)"
    }
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-SurgeLog "Skipped because another surge-alert process holds the mutex."
        exit 0
    }
    try {
        $hasMarketLock = $marketMutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMarketLock = $true
    }
    if (-not $hasMarketLock) {
        Write-SurgeLog "Skipped because market publication is still running; the next run will pick the fresh bars up."
        exit 0
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Dedicated Python environment not found: $python"
    }
    if (-not (Test-Path -LiteralPath $cli)) {
        throw "Trading CLI not found: $cli"
    }

    $arguments = @(
        $cli, "kol-surge-alerts",
        "--lookback-days", "$LookbackDays",
        "--window-days", "$WindowDays",
        "--threshold", "$Threshold"
    )
    if (-not $NoNotify) {
        $arguments += "--notify"
    }
    Write-SurgeLog "Starting surge-alert scan (lookback=$LookbackDays window=$WindowDays threshold=$Threshold)."
    $stdoutPath = Join-Path $logDirectory (".kol-surge-" + [guid]::NewGuid().ToString("N") + ".out")
    $stderrPath = Join-Path $logDirectory (".kol-surge-" + [guid]::NewGuid().ToString("N") + ".err")
    try {
        & $python @arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { "" }
        $output = @($stdout, $stderr) -join [Environment]::NewLine
    } finally {
        Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    }
    if ($output.Trim()) {
        Write-SurgeLog $output.Trim()
    }
    if ($exitCode -ne 0) {
        throw "trading_cli.py exited with code $exitCode"
    }
    Write-SurgeLog "Surge-alert scan completed."
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-SurgeLog "Surge-alert error: $message"
    if (-not $NoNotify) {
        Send-FailureNotification $message
    }
    exit 1
} finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    if ($hasMarketLock) {
        $marketMutex.ReleaseMutex()
    }
    $mutex.Dispose()
    $marketMutex.Dispose()
}

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = (Get-Date -Format "yyyy-MM-dd"),
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
$logDirectory = Join-Path $runtime "kol\logs"
$logPath = Join-Path $logDirectory "kol-tracker.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLReturnTrackerDaily")
$hasLock = $false

function Write-TrackerLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding UTF8
}

function Send-FailureNotification([string]$Message) {
    try {
        Send-HermesUtf8Message -Message "[KOL回测库] 定时任务失败：$Message" | Out-Null
    } catch {
        Write-TrackerLog "Hermes failure notification could not be sent: $($_.Exception.Message)"
    }
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-TrackerLog "Skipped because another tracker process holds the mutex."
        exit 0
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Dedicated Python environment not found: $python"
    }
    if (-not (Test-Path -LiteralPath $cli)) {
        throw "Trading CLI not found: $cli"
    }

    $arguments = @($cli, "kol-update", "--as-of", $AsOf)
    if (-not $NoNotify) {
        $arguments += "--notify"
    }
    Write-TrackerLog "Starting KOL update for $AsOf."
    $output = & $python @arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) {
        Write-TrackerLog $output.Trim()
    }
    if ($exitCode -ne 0) {
        throw "trading_cli.py exited with code $exitCode"
    }
    Write-TrackerLog "KOL update completed."
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-TrackerLog "Tracker error: $message"
    Send-FailureNotification $message
    exit 1
} finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

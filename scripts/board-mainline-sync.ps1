[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$Weekly
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs\boards"
$runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "board-mainline-$runStamp.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\MarketBoardMainline")
$hasLock = $false

function Write-BoardLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

function Invoke-BoardCli([string[]]$Arguments) {
    Write-BoardLog "Running: $($Arguments -join ' ')"
    $output = & $python $cli @Arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-BoardLog $output.Trim() }
    if ($exitCode -ne 0) {
        throw "$($Arguments[0]) exited with code $exitCode"
    }
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-BoardLog "Skipped: another board mainline job holds the mutex."
        exit 0
    }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    if (-not (Test-Path -LiteralPath $cli)) { throw "Trading CLI not found: $cli" }

    Invoke-BoardCli -Arguments @("market-board-sync", "--as-of", $AsOf)
    if ($Weekly) {
        Invoke-BoardCli -Arguments @(
            "market-board-backfill", "--resume", "--as-of", $AsOf, "--days", "320",
            "--board-type", "industry", "--batch-size", "100"
        )
        Invoke-BoardCli -Arguments @(
            "market-board-backfill", "--resume", "--as-of", $AsOf, "--days", "320",
            "--board-type", "concept", "--batch-size", "80"
        )
    }
    Invoke-BoardCli -Arguments @("market-board-rps", "--as-of", $AsOf)
    if ($Weekly) {
        Invoke-BoardCli -Arguments @("market-board-memberships", "--as-of", $AsOf, "--limit", "20")
    }
    Write-BoardLog "Board mainline job completed."
    if (-not $Weekly) {
        $researchTask = Get-ScheduledTask -TaskName "KOL_Event_Method_Research" -ErrorAction SilentlyContinue
        if ($researchTask) {
            Start-ScheduledTask -TaskName "KOL_Event_Method_Research"
            Write-BoardLog "Triggered event method research after board completion."
        } else {
            Write-BoardLog "Event method research task is not installed; the 02:00 fallback is unavailable."
        }
    }
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-BoardLog "Board mainline error: $message"
    try {
        Send-HermesUtf8Message -Message "[板块主线告警] $message。系统保留最后有效数据，未使用空结果重算。" | Out-Null
    } catch {
        Write-BoardLog "Hermes notification failed: $($_.Exception.Message)"
    }
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

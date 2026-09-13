[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = "",
    [string]$CandidateRoot = "",
    [switch]$NoReturnTrigger
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market-task-logs"
$logPath = Join-Path $logDirectory "market-daily-publish.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
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
    $report = Join-Path $RepoRoot "_runtime\trading\restore-reports\market-daily-publish-task.json"
    $runId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N")
    if (-not $CandidateRoot) {
        $candidateStamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $CandidateRoot = Join-Path $RepoRoot "_runtime\trading\market-live-candidate-$candidateStamp"
    }
    $arguments = @(
        $cli, "market-daily-publish",
        "--as-of", $(if ($AsOf) { $AsOf } else { "auto" }),
        "--apply", "--report", $report,
        "--candidate-root", $CandidateRoot
    )
    $maxAttempts = 3
    $runCompleted = $false
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $attemptId = "$runId-attempt-$attempt"
        $stdoutPath = Join-Path $logDirectory (".market-sync-$attemptId.out")
        $stderrPath = Join-Path $logDirectory (".market-sync-$attemptId.err")
        $exitCode = $null
        # Windows PowerShell promotes native stderr to an ErrorRecord. With
        # ErrorActionPreference=Stop that used to abort this block before the
        # exit code and traceback could be recorded.
        $savedErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $python @arguments 1> $stdoutPath 2> $stderrPath
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedErrorActionPreference
        }
        $stdout = [string]$(if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { "" })
        $stderr = [string]$(if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { "" })
        if (-not [string]::IsNullOrWhiteSpace($stdout)) { Write-MarketLog $stdout.Trim() }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) { Write-MarketLog ("stderr: " + $stderr.Trim()) }
        if ($exitCode -eq 0) {
            Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
            $runCompleted = $true
            break
        }

        $failureStdout = Join-Path $logDirectory "market-sync-failed-$attemptId.stdout.log"
        $failureStderr = Join-Path $logDirectory "market-sync-failed-$attemptId.stderr.log"
        if (Test-Path -LiteralPath $stdoutPath) { Move-Item -LiteralPath $stdoutPath -Destination $failureStdout -Force }
        if (Test-Path -LiteralPath $stderrPath) { Move-Item -LiteralPath $stderrPath -Destination $failureStderr -Force }
        Write-MarketLog "Preserved failed child output: stdout=$failureStdout stderr=$failureStderr"

        $fatalPython = $stderr -match "Fatal Python error|PyEval_SaveThread|current Python thread state is NULL"
        if ($fatalPython -and $attempt -lt $maxAttempts) {
            Write-MarketLog "Fatal BaoStock/Python failure on attempt $attempt; resuming candidate in a new Python process: $CandidateRoot"
            continue
        }
        throw "market-daily-publish exited with code $exitCode on attempt $attempt"
    }
    if (-not $runCompleted) { throw "market-daily-publish exhausted $maxAttempts attempts" }
    $startUi = Join-Path $RepoRoot "scripts\start-kol-ui.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startUi -RepoRoot $RepoRoot -Port 8123 -NoBrowser
    if ($LASTEXITCODE -ne 0) { throw "KOL UI restart failed after market publication" }
    if (-not $NoReturnTrigger -and (Get-ScheduledTask -TaskName "KOL_Return_Tracker_Daily" -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName "KOL_Return_Tracker_Daily"
        Write-MarketLog "Triggered KOL return update after successful market publication."
    }
    exit 0
} catch {
    Write-MarketLog "Market sync error: $($_.Exception.Message)"
    try {
        $startUi = Join-Path $RepoRoot "scripts\start-kol-ui.ps1"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startUi -RepoRoot $RepoRoot -Port 8123 -NoBrowser
        if ($LASTEXITCODE -ne 0) { Write-MarketLog "KOL UI recovery returned exit code $LASTEXITCODE" }
    } catch {
        Write-MarketLog "KOL UI recovery failed: $($_.Exception.Message)"
    }
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

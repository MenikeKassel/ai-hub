[CmdletBinding()]
param([string]$RepoRoot = "")

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\kol\logs"
$logPath = Join-Path $logDirectory "kol-review-agent.log"
$statePath = Join-Path $logDirectory "kol-review-agent-alert-state.json"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLReviewAgent")
$hasLock = $false

function Write-AgentLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

function Read-AgentState {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return [pscustomobject]@{ taskFailed = $false; modelFailureStreak = 0; modelAlerted = $false }
    }
    try { return Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return [pscustomobject]@{ taskFailed = $false; modelFailureStreak = 0; modelAlerted = $false } }
}

function Save-AgentState($State) {
    $State | ConvertTo-Json -Compress | Set-Content -LiteralPath $statePath -Encoding UTF8
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-AgentLog "Skipped: another review-agent run holds the mutex."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }

    $state = Read-AgentState
    $output = & $python $cli kol-review-agent-run --max-runtime 25 --max-items 20 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-AgentLog $output.Trim() }
    if ($exitCode -ne 0) { throw "kol-review-agent-run exited with code $exitCode" }

    $payload = $output | ConvertFrom-Json
    $processed = [int]$payload.counts.processed_count
    $failed = [int]$payload.counts.failed
    if ($processed -gt 0 -and $failed -ge $processed) {
        $state.modelFailureStreak = [int]$state.modelFailureStreak + 1
    } else {
        $state.modelFailureStreak = 0
        $state.modelAlerted = $false
    }
    if ($state.modelFailureStreak -ge 3 -and -not $state.modelAlerted) {
        try { Send-HermesUtf8Message -Message "KOL审核智能体告警：Codex连续三轮未能产生有效审核结果，请检查本地运行日志。" | Out-Null } catch { }
        $state.modelAlerted = $true
    }
    $state.taskFailed = $false
    Save-AgentState $state
    exit 0
} catch {
    Write-AgentLog "Review-agent error: $($_.Exception.Message)"
    $state = Read-AgentState
    if (-not $state.taskFailed) {
        try { Send-HermesUtf8Message -Message "KOL审核智能体告警：定时审核失败，请检查本地系统页和运行日志。" | Out-Null } catch { }
    }
    $state.taskFailed = $true
    Save-AgentState $state
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

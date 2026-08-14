[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$FetchCount = 20,
    [ValidateSet("all", "x", "zhihu")]
    [string]$Platform = "x"
)

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$logDirectory = Join-Path $RepoRoot "_runtime\trading\kol\logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$bootstrapPath = Join-Path $logDirectory ("morning-bootstrap-{0}-{1}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID)
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') bootstrap pid=$PID platform=$Platform repo=$RepoRoot" | Set-Content -LiteralPath $bootstrapPath -Encoding UTF8
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logPath = Join-Path $logDirectory "kol-morning-pipeline.log"
$runLog = Join-Path $logDirectory ("morning-{0}-{1}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID)
$stdoutPath = "$runLog.stdout"
$stderrPath = "$runLog.stderr"
$statePath = Join-Path $logDirectory "kol-morning-alert-state.json"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLDataPipeline")
$logMutex = New-Object System.Threading.Mutex($false, "Local\KOLMorningPipelineLog")
$hasLock = $false

function Write-PipelineLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    Add-Content -LiteralPath $runLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
    $hasLogLock = $false
    try {
        $hasLogLock = $logMutex.WaitOne(30000)
        if (-not $hasLogLock) { return }
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
            Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
        }
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
    } finally {
        if ($hasLogLock) { $logMutex.ReleaseMutex() }
    }
}

try {
    Write-PipelineLog "Bootstrap complete; waiting for data mutex. Python=$python"
    $hasLock = $mutex.WaitOne([TimeSpan]::FromMinutes(30))
    if (-not $hasLock) { Write-PipelineLog "Skipped: another data task held the mutex for 30 minutes."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $python $cli kol-morning-orchestrate --platform $Platform --fetch-count $FetchCount --provider nitter 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { "" }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($stdout.Trim()) { Write-PipelineLog ("stdout: " + $stdout.Trim()) }
    if ($stderr.Trim()) { Write-PipelineLog ("stderr: " + ($stderr.Trim() | Select-Object -Last 20)) }
    if ($exitCode -ne 0) { throw "kol-morning-orchestrate exited with code $exitCode" }
    '{"failed":false}' | Set-Content -LiteralPath $statePath -Encoding UTF8
    exit 0
} catch {
    Write-PipelineLog "Morning pipeline error: $($_.Exception.Message)"
    $alreadyFailed = $false
    if (Test-Path -LiteralPath $statePath) {
        try { $alreadyFailed = [bool](Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json).failed } catch { }
    }
    if (-not $alreadyFailed) {
        try { Send-HermesUtf8Message -Message "KOL morning review pipeline failed. Check Data Health and kol-morning-pipeline.log." | Out-Null } catch { }
    }
    '{"failed":true}' | Set-Content -LiteralPath $statePath -Encoding UTF8
    exit 1
} finally {
    Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    $logMutex.Dispose()
}

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$FetchCount = 20,
    [ValidateSet("all", "x", "zhihu")]
    [string]$Platform = "x",
    [switch]$NoNotify,
    [switch]$NoFetch
)

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$logDirectory = Join-Path $RepoRoot "_runtime\trading\kol\logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$bootstrapPath = Join-Path $logDirectory ("morning-bootstrap-{0}-{1}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID)
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') bootstrap pid=$PID platform=$Platform repo=$RepoRoot" | Set-Content -LiteralPath $bootstrapPath -Encoding UTF8
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")
$userToolBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path -LiteralPath $userToolBin) { $env:PATH = "$userToolBin;$env:PATH" }
$env:PYTHON_KEYRING_BACKEND = "keyring.backends.Windows.WinVaultKeyring"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$zhihuChrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (Test-Path -LiteralPath $zhihuChrome) { $env:ZHIHU_BROWSER_PATH = $zhihuChrome }
$env:ZHIHU_PROFILE_DIRECTORY = "Default"
$env:ZHIHU_USER_DATA_DIR = Join-Path $env:LOCALAPPDATA "hermes\browser-profiles\zhihu-edge"
$env:ZHIHU_CDP_PORT = "9223"
$workspaceRoot = Split-Path -Parent $RepoRoot
$env:FREESTOCKDB_ROOT = Join-Path $workspaceRoot "freestock\stockdb"
$env:FREESTOCKDB_DATA_ROOT = $env:FREESTOCKDB_ROOT
$env:FREESTOCKDB_URL = "http://127.0.0.1:7899"

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
        $orchestrateArgs = @("$cli", "kol-morning-orchestrate", "--platform", $Platform, "--fetch-count", $FetchCount, "--provider", "auto")
        # The 08:45 scheduled all-platform phase is a review-only phase;
        # retain compatibility with older task XML by inferring that window
        # when -NoFetch was not yet registered by an elevated task update.
        $reviewOnlyWindow = $Platform -eq "all" -and (Get-Date).Hour -eq 8 -and (Get-Date).Minute -ge 40
        if ($NoFetch -or $reviewOnlyWindow) { $orchestrateArgs += "--skip-fetch" }
        & $python @orchestrateArgs 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { "" }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($stdout -and $stdout.Trim()) { Write-PipelineLog ("stdout: " + $stdout.Trim()) }
    if ($stderr -and $stderr.Trim()) { Write-PipelineLog ("stderr: " + ($stderr.Trim() | Select-Object -Last 20)) }
    # The orchestrator uses exit code 2 for a completed, degraded run (for
    # example one account rate-limited while the remaining accounts succeed).
    # Treat that as a successful operator run when its JSON payload is ok=true;
    # only a hard process failure should trip the task error path.
    $completedDegraded = $false
    if ($exitCode -eq 2 -and $stdout) {
        try { $completedDegraded = [bool](($stdout | ConvertFrom-Json).ok) } catch { $completedDegraded = $false }
    }
    if ($exitCode -ne 0 -and -not $completedDegraded) { throw "kol-morning-orchestrate exited with code $exitCode" }
    '{"failed":false}' | Set-Content -LiteralPath $statePath -Encoding UTF8
    exit 0
} catch {
    Write-PipelineLog "Morning pipeline error: $($_.Exception.Message)"
    $alreadyFailed = $false
    if (Test-Path -LiteralPath $statePath) {
        try { $alreadyFailed = [bool](Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json).failed } catch { }
    }
    if (-not $alreadyFailed -and -not $NoNotify) {
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

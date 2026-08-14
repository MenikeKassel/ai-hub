[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = (Get-Date -Format "yyyy-MM-dd"),
    [ValidateSet("all", "x", "zhihu")]
    [string]$Platform = "x",
    [int]$FetchCount = 50,
    [switch]$NoNotify,
    [switch]$SkipAiPrefill,
    [switch]$NotifyOnCompletion
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\kol\logs"
$logPath = Join-Path $logDirectory "kol-post-fetch.log"
$pidPath = Join-Path $logDirectory "kol-post-fetch.pid"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLDataPipeline")
$hasLock = $false

# Scheduled tasks do not always inherit Python's backend discovery state. Force
# the native Windows vault and UTF-8 before Python or twitter-cli is launched.
$env:PYTHON_KEYRING_BACKEND = "keyring.backends.Windows.WinVaultKeyring"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Write-FetchLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-FetchLog "Skipped: another KOL data task holds the mutex."; exit 0 }
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    Set-Content -LiteralPath $pidPath -Value $PID -Encoding ASCII
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    if (-not (Test-Path -LiteralPath $cli)) { throw "Trading CLI not found: $cli" }

    $batchKey = "manual:$AsOf`:$Platform`:$((Get-Date).ToString('yyyyMMddHH'))"
    # ACCOUNT SAFETY (2026-08-14): nitter-only. twitter-cli credential path disabled
    # (user's X account warned); trading_cli._post_provider refuses auto/twitter.
    $arguments = @($cli, "kol-post-fetch", "--provider", "nitter", "--platform", $Platform, "--backfill", $FetchCount, "--as-of", $AsOf, "--batch-key", $batchKey, "--skip-classify")
    & $python $cli kol-post-db-backup *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to back up posts.db before fetch." }
    if (-not $NoNotify) { $arguments += @("--notify", "--alerts-only") }
    Write-FetchLog "Starting $Platform post fetch for $AsOf."
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $python @arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($output.Trim()) { Write-FetchLog $output.Trim() }
    if ($exitCode -ne 0) { throw "kol-post-fetch exited with code $exitCode" }
    if (-not $SkipAiPrefill) {
        $previewDate = (Get-Date).AddDays(1).ToString("yyyy-MM-dd")
        Write-FetchLog "Starting optional AI prefill for the $previewDate morning preview."
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $preview = & $python $cli kol-morning-run --as-of $previewDate --phase preview --skip-fetch --max-runtime 40 --backlog-limit 0 2>&1 | Out-String
            $previewExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($preview.Trim()) { Write-FetchLog $preview.Trim() }
        if ($previewExitCode -ne 0) {
            Write-FetchLog "AI prefill degraded with exit code $previewExitCode. Collected posts remain available for rules-only and manual review."
        }
    } else {
        Write-FetchLog "Skipped AI prefill by request."
    }
    Write-FetchLog "$Platform post fetch completed."
    if ($NotifyOnCompletion) {
        try {
            $resultLine = @($output -split "\r?\n" | Where-Object { $_.Trim() })[-1]
            $payload = $resultLine | ConvertFrom-Json
            $summary = "[KOL采集完成] 平台 $Platform，成功账号 $($payload.successful_kols)，失败账号 $($payload.failed_kols)，新增帖子 $($payload.new_posts)，候选荐股 $($payload.candidate_posts)，待审核 $($payload.pending_reviews)。"
            Send-HermesUtf8Message -Message $summary | Out-Null
        } catch {
            Write-FetchLog "Completion notification failed: $($_.Exception.Message)"
        }
    }
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-FetchLog "Fetch error: $message"
    try { Send-HermesUtf8Message -Message "[KOL自动采集] 定时任务失败：$message" | Out-Null } catch { }
    exit 1
} finally {
    if (Test-Path -LiteralPath $pidPath) {
        try {
            $owner = (Get-Content -LiteralPath $pidPath -Raw).Trim()
            if ($owner -eq [string]$PID) { Remove-Item -LiteralPath $pidPath -Force }
        } catch { }
    }
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

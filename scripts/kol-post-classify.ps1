[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$DailyLimit = 250,
    [int]$OcrLimit = 150
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
. (Join-Path $PSScriptRoot "lib\hermes-notify.ps1")

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\kol\logs"
$logPath = Join-Path $logDirectory "kol-post-classify.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLDataPipeline")
$hasLock = $false
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Write-ClassifyLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

function Invoke-Captured([string[]]$Arguments) {
    $stdoutPath = Join-Path $logDirectory (".kol-classify-" + [guid]::NewGuid().ToString("N") + ".out")
    $stderrPath = Join-Path $logDirectory (".kol-classify-" + [guid]::NewGuid().ToString("N") + ".err")
    try {
        & $python @Arguments 1> $stdoutPath 2> $stderrPath
        $code = $LASTEXITCODE
        $stdout = [string](if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8 } else { "" })
        $stderr = [string](if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8 } else { "" })
        if ($stdout.Trim()) { Write-ClassifyLog $stdout.Trim() }
        if ($stderr.Trim()) { Write-ClassifyLog ("stderr: " + $stderr.Trim()) }
        return $code
    } finally {
        Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    }
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-ClassifyLog "Skipped: another classifier holds the mutex."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    $recoveryCode = Invoke-Captured @($cli, "kol-ai-queue-maintain", "--daily-limit", $DailyLimit, "--recover-stale", "--apply")
    if ($recoveryCode -ne 0) { throw "stale queue recovery exited with code $recoveryCode" }
    $exitCode = Invoke-Captured @($cli, "kol-post-classify", "--pending", "--limit", "0", "--daily-limit", $DailyLimit, "--ocr-limit", $OcrLimit)
    if ($exitCode -ne 0) { throw "kol-post-classify exited with code $exitCode" }
    exit 0
} catch {
    Write-ClassifyLog "Classification error: $($_.Exception.Message)"
    try { Send-HermesUtf8Message -Message "[KOL classifier alert] Scheduled classification failed. Check the local console." | Out-Null } catch { }
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

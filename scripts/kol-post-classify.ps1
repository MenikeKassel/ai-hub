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

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-ClassifyLog "Skipped: another classifier holds the mutex."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    $output = & $python $cli kol-post-classify --pending --limit 0 --daily-limit $DailyLimit --ocr-limit $OcrLimit 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-ClassifyLog $output.Trim() }
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

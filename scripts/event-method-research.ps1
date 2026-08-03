[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$MaxEvents = 25
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs\event-research"
$runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "event-research-$runStamp.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\KOLEventMethodResearch")
$hasLock = $false

function Write-ResearchLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-ResearchLog "Skipped: another event research run holds the mutex."
        exit 0
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Trading Python not found: $python"
    }
    $output = & $python $cli kol-method-research-run --pending --max-events $MaxEvents --with-minute 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-ResearchLog $output.Trim() }
    if ($exitCode -eq 3 -and $output -match '"status":\s*"busy"') {
        Write-ResearchLog "Skipped: a manual or scheduled research run already owns the file lock."
        exit 0
    }
    if ($exitCode -ne 0) {
        throw "kol-method-research-run exited with code $exitCode"
    }
    exit 0
} catch {
    Write-ResearchLog "Event research error: $($_.Exception.Message)"
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

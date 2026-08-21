[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$AsOf = (Get-Date -Format "yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs"
$logPath = Join-Path $logDirectory "market-data-consumer.log"
$foundationPointer = "F:\ai-data\ashare\current.json"
$consumerState = Join-Path $RepoRoot "_runtime\trading\market\foundation-consumer-state.json"
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
    if (-not (Test-Path -LiteralPath $foundationPointer)) {
        Write-MarketLog "Skipped: unified foundation current.json is missing."
        exit 0
    }
    $releaseId = ((Get-Content -LiteralPath $foundationPointer -Raw -Encoding UTF8) | ConvertFrom-Json).release_id
    $previousRelease = ""
    if (Test-Path -LiteralPath $consumerState) {
        try { $previousRelease = (Get-Content -LiteralPath $consumerState -Raw -Encoding UTF8 | ConvertFrom-Json).release_id } catch { $previousRelease = "" }
    }
    if ($releaseId -and $releaseId -eq $previousRelease) {
        Write-MarketLog "Skipped: foundation release $releaseId already consumed."
        exit 0
    }
    # The unified foundation owns all daily fact writes. This legacy task is a
    # read-only consumer that rebuilds derived event context from current.json.
    $output = & $python $cli market-sync --as-of $AsOf --alerts-only 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-MarketLog $output.Trim() }
    if ($exitCode -ne 0) { throw "read-only market consumer exited with code $exitCode" }
    $state = @{ release_id = $releaseId; consumed_at = (Get-Date).ToString("o") } | ConvertTo-Json
    $stateTemp = "$consumerState.tmp"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $consumerState) | Out-Null
    Set-Content -LiteralPath $stateTemp -Value $state -Encoding UTF8
    Move-Item -LiteralPath $stateTemp -Destination $consumerState -Force
    exit 0
} catch {
    Write-MarketLog "Market sync error: $($_.Exception.Message)"
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

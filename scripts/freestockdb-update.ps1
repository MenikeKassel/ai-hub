[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$DataRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
if (-not $DataRoot) {
    $DataRoot = if ($env:FREESTOCKDB_DATA_ROOT) { $env:FREESTOCKDB_DATA_ROOT } else { Join-Path $RepoRoot "..\freestock\stockdb" }
}
$workspaceRoot = Split-Path -Parent $RepoRoot
$env:FREESTOCKDB_ROOT = Join-Path $workspaceRoot "freestock\stockdb"
$env:FREESTOCKDB_DATA_ROOT = $DataRoot
$env:FREESTOCKDB_URL = "http://127.0.0.1:7899"

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$logDirectory = Join-Path $RepoRoot "_runtime\trading\market\logs"
$logPath = Join-Path $logDirectory "freestockdb-update-task.log"
$mutex = New-Object System.Threading.Mutex($false, "Local\FreeStockDBUpdateDaily")
$hasLock = $false

# Hermes and desktop launchers may inject a Python runtime into child
# processes. This task must use only the dedicated trading environment.
$env:PYTHONPATH = ""
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONSTARTUP -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONUSERBASE -ErrorAction SilentlyContinue

function Write-UpdateLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { Write-UpdateLog "Skipped: another FreeStockDB update holds the mutex."; exit 0 }
    if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
    $targetDate = ""
    $foundationRoot = Join-Path $workspaceRoot "ashare-data-foundation"
    $foundationPython = Join-Path $foundationRoot ".venv\Scripts\python.exe"
    $foundationCli = Join-Path $foundationRoot "src"
    if (Test-Path -LiteralPath $foundationPython) {
        $env:PYTHONPATH = $foundationCli
        $targetOutput = & $foundationPython -m ashare_data_foundation.cli --data-root $DataRoot target-date --as-of auto 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            try { $targetDate = (($targetOutput | ConvertFrom-Json).date).ToString() } catch { $targetDate = "" }
        }
        $env:PYTHONPATH = ""
    }
    $doctorArguments = @($cli, "market-freestockdb-doctor", "--root", $env:FREESTOCKDB_ROOT, "--data-root", $DataRoot)
    if ($targetDate) { $doctorArguments += @("--expected-date", $targetDate) }
    $doctorOutput = & $python @doctorArguments 2>&1 | Out-String
    $doctor = $doctorOutput | ConvertFrom-Json
    if (-not $doctor.service_ok -or $doctor.connection_leak) {
        Write-UpdateLog "Runtime unhealthy before update; running verified repair."
        $repairOutput = & $python $cli market-freestockdb-repair 2>&1 | Out-String
        $repairExitCode = $LASTEXITCODE
        if ($repairOutput.Trim()) { Write-UpdateLog $repairOutput.Trim() }
        if ($repairExitCode -ne 0) {
            Write-UpdateLog "First repair pass did not recover the service; running the second verified pass."
            $repairOutput = & $python $cli market-freestockdb-repair 2>&1 | Out-String
            $repairExitCode = $LASTEXITCODE
            if ($repairOutput.Trim()) { Write-UpdateLog $repairOutput.Trim() }
        }
        if ($repairExitCode -ne 0) { throw "FreeStockDB repair failed before update." }
        $doctorOutput = & $python @doctorArguments 2>&1 | Out-String
        $doctor = $doctorOutput | ConvertFrom-Json
    }
    if ($doctor.service_ok -and -not $doctor.disk.update_guard_ok) {
        $required = $doctor.disk.required_for_safe_update_gb
        Write-UpdateLog "Skipped: safe staged update needs $required GB free; service remains available."
        exit 0
    }
    if (-not $doctor.update_ready) {
        $failedChecks = @(
            $doctor.checks.PSObject.Properties |
                Where-Object { $_.Value -eq $false } |
                Select-Object -ExpandProperty Name
        ) -join ","
        throw "FreeStockDB update preflight failed: $failedChecks"
    }
    $arguments = @($cli, "market-freestockdb-update", "--timeout", "3300", "--root", $env:FREESTOCKDB_ROOT, "--data-root", $DataRoot)
    if ($targetDate) { $arguments += @("--expected-date", $targetDate) }
    if ($DryRun) { $arguments += "--dry-run" }
    $output = & $python @arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($output.Trim()) { Write-UpdateLog $output.Trim() }
    if ($exitCode -ne 0) { throw "market-freestockdb-update exited with code $exitCode" }
    if (-not $DryRun) {
        $doctorOutput = & $python @doctorArguments 2>&1 | Out-String
        $doctorExitCode = $LASTEXITCODE
        if ($doctorOutput.Trim()) { Write-UpdateLog $doctorOutput.Trim() }
        if ($doctorExitCode -ne 0) { throw "FreeStockDB freshness verification failed after update." }
    }
    exit 0
} catch {
    Write-UpdateLog "FreeStockDB update error: $($_.Exception.Message)"
    exit 1
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

[CmdletBinding()]
param(
    [string]$HermesCommand = "hermes",
    [string]$LogPath = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "_runtime\watchdog\hermes-watchdog.log")
)

$ErrorActionPreference = "Stop"
$mutex = New-Object System.Threading.Mutex($false, "Local\HermesGatewayWatchdog")
$hasLock = $false

function Write-WatchdogLog([string]$Message) {
    $directory = Split-Path -Parent $LogPath
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    if ((Test-Path -LiteralPath $LogPath) -and (Get-Item -LiteralPath $LogPath).Length -gt 1MB) {
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.1" -Force
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Value "$timestamp $Message" -Encoding UTF8
}

function Test-GatewayRunning {
    $status = & $HermesCommand status --deep 2>&1 | Out-String
    return ($LASTEXITCODE -eq 0 -and $status -match "Gateway Service[\s\S]*Status:\s+.*running")
}

try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        exit 0
    }

    if (Test-GatewayRunning) {
        exit 0
    }

    Write-WatchdogLog "Gateway is stopped; starting from external watchdog."
    & $HermesCommand gateway start *> $null
    Start-Sleep -Seconds 15

    if (Test-GatewayRunning) {
        Write-WatchdogLog "Gateway recovered."
        exit 0
    }

    Write-WatchdogLog "Gateway start was attempted but health check still failed."
    exit 1
} catch {
    Write-WatchdogLog "Watchdog error: $($_.Exception.Message)"
    exit 1
} finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$FreeStockRoot = "",
    [int]$Port = 7899
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$workspaceRoot = Split-Path -Parent $RepoRoot
if (-not $FreeStockRoot) {
    $FreeStockRoot = if ($env:FREESTOCKDB_ROOT) {
        $env:FREESTOCKDB_ROOT
    } else {
        Join-Path $workspaceRoot "freestock\stockdb"
    }
}
$FreeStockRoot = (Resolve-Path -LiteralPath $FreeStockRoot).Path
$server = Join-Path $FreeStockRoot "stockdb.exe"
$config = Join-Path $FreeStockRoot "stockdb.conf"
$data = Join-Path $FreeStockRoot "data"
$manifest = Join-Path $data ".sync_manifest.json"
$runtime = Join-Path $RepoRoot "_runtime\trading\market"
$metadataPath = Join-Path $runtime "freestockdb-process.json"
$stopRequestPath = Join-Path $runtime "freestockdb-stop.request.json"
$stopResultPath = Join-Path $runtime "freestockdb-stop.result.json"
$url = "http://127.0.0.1:$Port"

function Get-ListenerProcessIds {
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Get-ExactProcesses {
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name='stockdb.exe'" |
            Where-Object {
                $exe = [string]$_.ExecutablePath
                ($exe -and $exe -ieq $server) -or
                ([string]$_.CommandLine -match [regex]::Escape($server))
            })
    } catch {
        return @()
    }
}

function Get-ServerListenerProcessIds {
    # The elevated server process can hide ExecutablePath/CommandLine from a
    # limited-token query, so identify our own listener by process name.
    $matches = @()
    foreach ($listenerPid in @(Get-ListenerProcessIds)) {
        $proc = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -ieq [IO.Path]::GetFileNameWithoutExtension($server)) {
            $matches += [int]$listenerPid
        }
    }
    return @($matches)
}

function Test-RecordedListener {
    param([int[]]$Listeners)
    if (-not (Test-Path -LiteralPath $metadataPath)) { return $false }
    try {
        $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $recordedPid = [int]$metadata.pid
        $recordedRoot = [IO.Path]::GetFullPath([string]$metadata.root).TrimEnd('\')
        $expectedRoot = [IO.Path]::GetFullPath($FreeStockRoot).TrimEnd('\')
        return (
            $recordedPid -gt 0 -and
            $Listeners -contains $recordedPid -and
            $recordedRoot -ieq $expectedRoot -and
            $null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)
        )
    } catch {
        return $false
    }
}

# The service can be started by this elevated logon task while the updater runs
# with a limited token. Let the owner task stop its own recorded listener before
# an atomic dataset swap.
if (Test-Path -LiteralPath $stopRequestPath) {
    $request = Get-Content -LiteralPath $stopRequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Remove-Item -LiteralPath $stopRequestPath -Force -ErrorAction SilentlyContinue
    try {
        if (-not (Test-Path -LiteralPath $metadataPath)) {
            throw "FreeStockDB process metadata is missing."
        }
        $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $recordedPid = [int]$metadata.pid
        $recordedRoot = [IO.Path]::GetFullPath([string]$metadata.root).TrimEnd('\')
        $expectedRoot = [IO.Path]::GetFullPath($FreeStockRoot).TrimEnd('\')
        $listeners = @(Get-ListenerProcessIds)
        if ($recordedRoot -ine $expectedRoot) {
            throw "Recorded FreeStockDB root does not match $FreeStockRoot."
        }
        # The recorded pid can drift from the pid that actually serves the port
        # (the launcher process exits once the server takes over; observed
        # 2026-09-16: recorded 16420 vs listener 16424). Prefer the recorded pid
        # while it still owns the port, otherwise stop the listener that matches
        # our own server binary — and refuse anything else.
        $targets = @()
        if ($recordedPid -gt 0 -and $listeners -contains $recordedPid) {
            $targets = @($recordedPid)
        } else {
            $targets = @(Get-ServerListenerProcessIds)
        }
        if ($targets.Count -eq 0) {
            throw "No FreeStockDB listener owns port $Port (listeners: $($listeners -join ', '))."
        }
        foreach ($targetPid in $targets) {
            $target = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if ($target) {
                $target | Stop-Process -Force -ErrorAction Stop
                $target | Wait-Process -Timeout 15 -ErrorAction Stop
            }
        }
        Remove-Item -LiteralPath $metadataPath -Force -ErrorAction SilentlyContinue
        [ordered]@{ token = $request.token; status = "stopped"; completed_at = [DateTimeOffset]::Now.ToString("o") } |
            ConvertTo-Json -Compress | Set-Content -LiteralPath $stopResultPath -Encoding UTF8
        exit 0
    } catch {
        [ordered]@{ token = $request.token; status = "failed"; error = $_.Exception.Message; completed_at = [DateTimeOffset]::Now.ToString("o") } |
            ConvertTo-Json -Compress | Set-Content -LiteralPath $stopResultPath -Encoding UTF8
        exit 1
    }
}

if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "FreeStockDB server not found: $server" }
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw "FreeStockDB config not found: $config" }
if (-not (Test-Path -LiteralPath $data -PathType Container)) { throw "FreeStockDB data directory not found: $data" }
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "FreeStockDB data manifest not found: $manifest" }
$manifestValue = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
if (@($manifestValue.files).Count -lt 1) { throw "FreeStockDB data manifest is empty: $manifest" }

$env:FREESTOCKDB_ROOT = $FreeStockRoot
$env:FREESTOCKDB_DATA_ROOT = $FreeStockRoot
$env:FREESTOCKDB_URL = $url

$listeners = @(Get-ListenerProcessIds)
if ($listeners.Count -gt 0) {
    $exact = @(Get-ExactProcesses)
    $serverListeners = @(Get-ServerListenerProcessIds)
    $metadataRootMatches = $false
    if (Test-Path -LiteralPath $metadataPath) {
        try {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $metadataRootMatches = (
                [IO.Path]::GetFullPath([string]$metadata.root).TrimEnd('\') -ieq
                [IO.Path]::GetFullPath($FreeStockRoot).TrimEnd('\')
            )
        } catch {
            $metadataRootMatches = $false
        }
    }
    if ($exact.Count -gt 0 -or (Test-RecordedListener -Listeners $listeners) -or ($serverListeners.Count -gt 0 -and $metadataRootMatches)) {
        Write-Host "FreeStockDB is already running: $url"
        exit 0
    }
    throw "FreeStockDB port $Port is occupied by process ID(s): $($listeners -join ', ')."
}

New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$process = Start-Process -FilePath $server -WorkingDirectory $FreeStockRoot -WindowStyle Hidden -PassThru
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    $exact = @(Get-CimInstance Win32_Process -Filter "Name='stockdb.exe'" |
        Where-Object { [string]$_.ExecutablePath -ieq $server })
    $serverListeners = @(Get-ServerListenerProcessIds)
    if ((Get-ListenerProcessIds).Count -gt 0 -and ($exact.Count -gt 0 -or $serverListeners.Count -gt 0)) {
        $ready = $true
        break
    }
    if ($process.HasExited) { break }
}
if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    throw "FreeStockDB failed to start from $FreeStockRoot (port $Port)."
}

$listenerPids = @(Get-ListenerProcessIds)
$servingPid = if ($listenerPids.Count -gt 0) { [int]$listenerPids[0] } else { [int]$process.Id }
$metadata = [ordered]@{
    pid = $servingPid
    launcher_pid = [int]$process.Id
    root = $FreeStockRoot
    data = $data
    url = $url
    started_at = [DateTimeOffset]::Now.ToString("o")
}
$metadata | ConvertTo-Json -Compress | Set-Content -LiteralPath $metadataPath -Encoding UTF8
Write-Host "FreeStockDB: $url (pid=$servingPid, data=$data)"

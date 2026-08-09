[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$Port = 8125,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$appDirectory = Join-Path $RepoRoot "_automation\trading_research"
$runtime = Join-Path $RepoRoot "_runtime\trading\kol\discovery-ui"
$stdout = Join-Path $runtime "server.out.log"
$stderr = Join-Path $runtime "server.err.log"
$pidPath = Join-Path $runtime "server.pid"
$metadataPath = Join-Path $runtime "server.process.json"
$url = "http://127.0.0.1:$Port"
$mutexName = "Global\ai-hub-kol-discovery-$Port"

function Get-HttpReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/api/v1/pipeline/status" -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-ListenerProcess {
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Get-ErrorTail {
    if (-not (Test-Path -LiteralPath $stderr)) { return "" }
    return ((Get-Content -LiteralPath $stderr -Tail 80 -ErrorAction SilentlyContinue) -join [Environment]::NewLine)
}

if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$mutexAcquired = $false
try {
    $mutexAcquired = $mutex.WaitOne(0)
    if (-not $mutexAcquired) { throw "Another KOL discovery start operation is already in progress." }

    if (Get-HttpReady) {
        if (-not $NoBrowser) { Start-Process $url }
        Write-Host "KOL discovery console is already running: $url"
        exit 0
    }
    $listeners = @(Get-ListenerProcess)
    if ($listeners.Count -gt 0) {
        throw "Port $Port is already occupied by process ID(s): $($listeners -join ', ')."
    }

    $identity = & $python -c "import json,sys; print(json.dumps({'executable':sys.executable,'version':sys.version.split()[0]}))"
    if ($LASTEXITCODE -ne 0) { throw "Trading Python identity check failed." }
    $identity = ($identity | Select-Object -Last 1 | ConvertFrom-Json)

    & $python (Join-Path $appDirectory "trading_cli.py") kol-post-db-backup *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to back up posts.db before discovery startup." }

    $process = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "kol_discovery_runtime:create_private_app", "--factory",
        "--host", "127.0.0.1", "--port", [string]$Port
    ) -WorkingDirectory $appDirectory -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII
    [ordered]@{
        pid = $process.Id
        port = $Port
        python = $identity.executable
        python_version = $identity.version
        started_at = [DateTimeOffset]::Now.ToString("o")
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $metadataPath -Encoding UTF8

    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        Start-Sleep -Seconds 1
        if (Get-HttpReady) { $ready = $true; break }
        if ($process.HasExited) { break }
    }
    if (-not $ready) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        throw "KOL discovery failed to start. stderr=$((Get-ErrorTail))"
    }
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host "KOL discovery console: $url (pid=$($process.Id))"
} finally {
    if ($mutexAcquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

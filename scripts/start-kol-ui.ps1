[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$Port = 8123,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$workspaceRoot = Split-Path -Parent $RepoRoot
$env:FREESTOCKDB_ROOT = Join-Path $workspaceRoot "stockdb"
$env:FREESTOCKDB_DATA_ROOT = "<MARKET_DATA_HOME>\free-stockdb"

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$appDirectory = Join-Path $RepoRoot "_automation\trading_research"
$index = Join-Path $appDirectory "ui\dist\index.html"
$runtime = Join-Path $RepoRoot "_runtime\trading\kol\ui"
$stdout = Join-Path $runtime "server.out.log"
$stderr = Join-Path $runtime "server.err.log"
$url = "http://127.0.0.1:$Port"
$pidPath = Join-Path $runtime "server.pid"
$metadataPath = Join-Path $runtime "server.process.json"
$mutexName = "Global\ai-hub-kol-ui-$Port"

function Get-HttpReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/api/pipeline/status" -TimeoutSec 5
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

function Restore-ProcessEnvironment {
    param([hashtable]$Saved)
    foreach ($name in $Saved.Keys) {
        [System.Environment]::SetEnvironmentVariable(
            $name,
            $Saved[$name],
            [System.EnvironmentVariableTarget]::Process
        )
    }
}

# Hermes and some agent sandboxes can inject both `Path` and `PATH`. Windows
# accepts that environment block, but Windows PowerShell 5 Start-Process builds
# a case-insensitive dictionary and fails on the duplicate key.
$processEnvironment = [System.Environment]::GetEnvironmentVariables()
if ($processEnvironment.Contains("Path") -and $processEnvironment.Contains("PATH")) {
    [System.Environment]::SetEnvironmentVariable(
        "PATH",
        $null,
        [System.EnvironmentVariableTarget]::Process
    )
}

if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }
if (-not (Test-Path -LiteralPath $index)) { throw "UI build not found: $index" }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$mutexAcquired = $false
$savedPythonEnvironment = @{}
$pythonEnvironmentNames = @(
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "CONDA_PREFIX"
)

try {
    $mutexAcquired = $mutex.WaitOne(0)
    if (-not $mutexAcquired) {
        throw "Another KOL UI start operation is already in progress."
    }

    if (Get-HttpReady) {
        if (-not $NoBrowser) { Start-Process $url }
        Write-Host "KOL research console is already running: $url"
        exit 0
    }

    $listeners = @(Get-ListenerProcess)
    if ($listeners.Count -gt 0) {
        throw "Port $Port is already occupied by process ID(s): $($listeners -join ', ')."
    }

    foreach ($name in $pythonEnvironmentNames) {
        $savedPythonEnvironment[$name] = [System.Environment]::GetEnvironmentVariable(
            $name,
            [System.EnvironmentVariableTarget]::Process
        )
        [System.Environment]::SetEnvironmentVariable(
            $name,
            $null,
            [System.EnvironmentVariableTarget]::Process
        )
    }

    $identity = & $python -c "import json,sys; print(json.dumps({'executable':sys.executable,'version':sys.version.split()[0]}))"
    if ($LASTEXITCODE -ne 0) { throw "Trading Python identity check failed." }
    $identity = ($identity | Select-Object -Last 1 | ConvertFrom-Json)
    if ([IO.Path]::GetFullPath($identity.executable) -ne [IO.Path]::GetFullPath($python)) {
        throw "Trading Python mismatch: expected $python but got $($identity.executable)."
    }

    & $python (Join-Path $appDirectory "trading_cli.py") kol-post-db-backup *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to back up posts.db before UI startup." }

    $process = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "kol_api:app", "--host", "127.0.0.1", "--port", [string]$Port
    ) -WorkingDirectory $appDirectory -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII
    $metadata = [ordered]@{
        pid = $process.Id
        port = $Port
        python = $identity.executable
        python_version = $identity.version
        started_at = [DateTimeOffset]::Now.ToString("o")
    }
    $metadata | ConvertTo-Json -Compress | Set-Content -LiteralPath $metadataPath -Encoding UTF8

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if (Get-HttpReady) { $ready = $true; break }
        if ($process.HasExited) { break }
    }
    if (-not $ready) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        throw "KOL UI failed to start. interpreter=$($identity.executable); stderr=$((Get-ErrorTail))"
    }
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host "KOL research console: $url (pid=$($process.Id), python=$($identity.executable))"
} finally {
    if ($savedPythonEnvironment.Count -gt 0) {
        Restore-ProcessEnvironment $savedPythonEnvironment
    }
    if ($mutexAcquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

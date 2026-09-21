[CmdletBinding()]
param([string]$RepoRoot = "")

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
$compose = Join-Path $RepoRoot "_infra\nitter\compose.yml"
$runtime = Join-Path $RepoRoot "_runtime\trading\kol\nitter"
$config = Join-Path $runtime "nitter.conf"
$sessions = Join-Path $runtime "sessions.jsonl"

foreach ($path in @($python, $cli, $compose)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path not found: $path" }
}
$docker = (Get-Command docker.exe -ErrorAction SilentlyContinue).Source
if (-not $docker) { $docker = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe" }
if (-not (Test-Path -LiteralPath $docker)) { throw "Docker Desktop is not installed." }
$env:PATH = "$(Split-Path $docker -Parent);$env:PATH"

function Test-DockerReady {
    # A stopped or starting Docker daemon is an expected readiness state.  Use
    # Process directly so Windows PowerShell cannot promote native stderr to a
    # terminating error, and cap each probe because docker.exe can otherwise
    # wait indefinitely for the named pipe.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $docker
    $startInfo.Arguments = "info"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $probe = New-Object System.Diagnostics.Process
    $probe.StartInfo = $startInfo
    try {
        if (-not $probe.Start()) { return $false }
        $stdoutTask = $probe.StandardOutput.ReadToEndAsync()
        $stderrTask = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit(5000)) {
            $probe.Kill()
            $probe.WaitForExit()
            return $false
        }
        $stdoutTask.GetAwaiter().GetResult() | Out-Null
        $stderrTask.GetAwaiter().GetResult() | Out-Null
        return ($probe.ExitCode -eq 0)
    } finally {
        $probe.Dispose()
    }
}

$dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
if (-not (Test-DockerReady) -and (Test-Path -LiteralPath $dockerDesktop)) {
    $desktopProcess = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $desktopProcess) {
        Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
    }
}

$ready = $false
$readyDeadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $readyDeadline) {
    if (Test-DockerReady) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw "Docker Desktop did not become ready within 120 seconds." }

& $python $cli kol-nitter-materialize | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Unable to materialize the Nitter session." }
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
foreach ($path in @($config, $sessions, (Join-Path $runtime "hmac.key"))) {
    & icacls.exe $path /inheritance:r /grant:r "${currentUser}:(F)" "SYSTEM:(F)" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to restrict access to $path" }
}

$env:NITTER_CONFIG_PATH = $config.Replace('\', '/')
$env:NITTER_SESSIONS_PATH = $sessions.Replace('\', '/')
& $docker compose -f $compose up -d nitter-redis
if ($LASTEXITCODE -eq 0) {
    & $docker compose -f $compose up -d --no-deps --force-recreate nitter
}
if ($LASTEXITCODE -ne 0) { throw "Unable to start the local Nitter stack." }

$healthy = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:9377" -TimeoutSec 3
        if ($response.StatusCode -lt 500) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $healthy) { throw "Nitter did not become ready. Run: docker logs nitter" }
Write-Host "Nitter is ready at http://127.0.0.1:9377"

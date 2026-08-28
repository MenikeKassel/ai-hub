[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$existingDocker = (Get-Command docker.exe -ErrorAction SilentlyContinue).Source
if (-not $existingDocker) { $existingDocker = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe" }
if (Test-Path -LiteralPath $existingDocker) {
    Write-Host "Docker is already installed."
    exit 0
}
$winget = (Get-Command winget.exe -ErrorAction SilentlyContinue).Source
if (-not $winget) {
    $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
}
if (-not (Test-Path -LiteralPath $winget)) {
    throw "winget is required to install Docker Desktop."
}
& $winget install --exact --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop installation failed with exit code $LASTEXITCODE." }
Write-Host "Docker Desktop was installed. A sign-out or restart may be required before first use."

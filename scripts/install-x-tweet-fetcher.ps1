[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$ProxyUrl = "http://127.0.0.1:7897"
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$source = Join-Path $RepoRoot "_external\x-tweet-fetcher"
$venv = Join-Path $RepoRoot "_runtime\venv-x-fetcher"
$python = Join-Path $venv "Scripts\python.exe"
$expectedCommit = "f057d6b42af9af5068194dde18ed44f1b1c30877"

if (-not (Test-Path -LiteralPath (Join-Path $source ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $source -Parent) | Out-Null
    & git -c "http.proxy=$ProxyUrl" -c "https.proxy=$ProxyUrl" clone --depth 1 --branch v3.0.0 https://github.com/ythx-101/x-tweet-fetcher.git $source
    if ($LASTEXITCODE -ne 0) { throw "Unable to clone x-tweet-fetcher v3.0.0." }
}
$actualCommit = (& git -C $source rev-parse HEAD).Trim()
if ($actualCommit -ne $expectedCommit) { throw "Unexpected x-tweet-fetcher commit: $actualCommit" }

if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { & $launcher.Source -3 -m venv $venv } else { & python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create isolated x-tweet-fetcher environment." }
}
& $python -m pip install --disable-pip-version-check --no-deps --force-reinstall $source
if ($LASTEXITCODE -ne 0) { throw "Unable to install x-tweet-fetcher." }
& $python -c "import importlib.metadata; assert importlib.metadata.version('x-tweet-fetcher') == '3.0.0'"
if ($LASTEXITCODE -ne 0) { throw "x-tweet-fetcher version verification failed." }
Set-Content -LiteralPath (Join-Path $venv ".ai-hub-version") -Value "v3.0.0 $expectedCommit" -Encoding ASCII
Write-Host "Installed x-tweet-fetcher v3.0.0 in $venv"

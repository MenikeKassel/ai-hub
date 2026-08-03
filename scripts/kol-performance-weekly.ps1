[CmdletBinding()]
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
$cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
if (-not (Test-Path -LiteralPath $python)) { throw "Trading Python not found: $python" }

$env:PYTHONPATH = ""
& $python $cli kol-performance-report --weekly --notify
if ($LASTEXITCODE -ne 0) { throw "KOL performance weekly report failed with exit code $LASTEXITCODE" }

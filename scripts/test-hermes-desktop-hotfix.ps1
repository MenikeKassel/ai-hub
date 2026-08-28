$ErrorActionPreference = "Stop"

$HermesRepo = "$env:LOCALAPPDATA\hermes\hermes-agent"
$Uv = "$env:LOCALAPPDATA\hermes\bin\uv.exe"

if (-not (Test-Path $HermesRepo)) {
    throw "Hermes repo not found: $HermesRepo"
}

if (-not (Test-Path $Uv)) {
    throw "uv not found: $Uv"
}

Push-Location $HermesRepo
try {
    & $Uv run --extra dev python -m pytest tests\hermes_cli\test_web_server_boot_handshake.py -q
} finally {
    Pop-Location
}

[CmdletBinding()]
param(
    [string]$HermesRepo = "<USER_HOME>\AppData\Local\hermes\hermes-agent",
    [string]$Uv = "<USER_HOME>\AppData\Local\hermes\bin\uv.exe"
)

$ErrorActionPreference = "Stop"

foreach ($required in @($HermesRepo, $Uv)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

Push-Location $HermesRepo
try {
    & $Uv run --extra dev --with aiohttp --with httpx python -m pytest `
        tests\gateway\test_qqbot.py::TestQQAdapterInit::test_connect_accepts_gateway_reconnect_contract `
        -q
    if ($LASTEXITCODE -ne 0) {
        throw "QQBot reconnect contract test failed."
    }
} finally {
    Pop-Location
}

$status = hermes status --deep | Out-String
if ($status -notmatch "QQBot\s+.*configured") {
    throw "QQBot is not reported as configured."
}
if ($status -notmatch "Gateway Service[\s\S]*Status:\s+.*running") {
    throw "Hermes Gateway is not running."
}

Write-Host "QQBot contract test passed; QQBot is configured and Gateway is running."

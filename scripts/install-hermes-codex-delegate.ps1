[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$HermesHome = "",
    [switch]$RestartGateway
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if (-not $HermesHome) {
    $HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }
}

$skillSource = Join-Path $RepoRoot "_skills\delegate-to-codex\SKILL.md"
$skillTargetDir = Join-Path $HermesHome "skills\delegate-to-codex"
$skillTarget = Join-Path $skillTargetDir "SKILL.md"
$pluginSource = Join-Path $RepoRoot "_automation\codex-delegate\hermes_plugin.py"
$pluginTargetDir = Join-Path $HermesHome "plugins\codex-delegate"
$pluginManifest = Join-Path $pluginTargetDir "plugin.yaml"
$pluginInit = Join-Path $pluginTargetDir "__init__.py"
$configPath = Join-Path $HermesHome "config.yaml"
$soulBlockPath = Join-Path $RepoRoot "_templates\hermes\SOUL.codex-routing.md"
$soulPath = Join-Path $HermesHome "SOUL.md"

foreach ($required in @($skillSource, $pluginSource, $soulBlockPath, $configPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required source file not found: $required"
    }
}

New-Item -ItemType Directory -Force -Path $skillTargetDir | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Force
Write-Host "Installed Hermes skill: $skillTarget"

New-Item -ItemType Directory -Force -Path $pluginTargetDir | Out-Null
$pluginManifestContent = @"
name: codex-delegate
version: 1.0.0
description: "Native bridge that delegates maintenance exclusively to the local Codex CLI."
author: "local"
provides_tools:
  - codex_delegate
"@
$pluginInitContent = @"
from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(r"$pluginSource")


def _load_impl():
    spec = importlib.util.spec_from_file_location("ai_hub_codex_delegate_plugin", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Codex delegate plugin from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:
    _load_impl().register(ctx)
"@
[System.IO.File]::WriteAllText($pluginManifest, $pluginManifestContent, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($pluginInit, $pluginInitContent, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Installed Hermes plugin: $pluginTargetDir"

$configContent = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
$pluginName = "codex-delegate"
if ($configContent -notmatch '(?m)^plugins:\s*$') {
    $configContent = $configContent.TrimEnd() + [Environment]::NewLine + @"
plugins:
  enabled:
    - $pluginName
"@ + [Environment]::NewLine
} elseif ($configContent -notmatch "(?m)^\s*-\s*$([regex]::Escape($pluginName))\s*$") {
    if ($configContent -notmatch '(?m)^\s{2}enabled:\s*$') {
        throw "Hermes plugins.enabled is not a block list. Add $pluginName manually."
    }
    $configContent = [regex]::Replace(
        $configContent,
        '(?m)^(\s{2}enabled:\s*)$',
        ('$1' + [Environment]::NewLine + "    - $pluginName"),
        1
    )
}
$configTempPath = "$configPath.codex-plugin.tmp"
[System.IO.File]::WriteAllText($configTempPath, $configContent, (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $configTempPath -Destination $configPath -Force

$block = (Get-Content -LiteralPath $soulBlockPath -Raw -Encoding UTF8).Trim()
$soul = if (Test-Path -LiteralPath $soulPath) {
    Get-Content -LiteralPath $soulPath -Raw -Encoding UTF8
} else {
    ""
}
$pattern = '(?s)<!-- ai-hub:codex-routing:start -->.*?<!-- ai-hub:codex-routing:end -->'
if ($soul -match $pattern) {
    $updatedSoul = [regex]::Replace($soul, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $block })
} else {
    $updatedSoul = $soul.TrimEnd() + "`r`n`r`n" + $block + "`r`n"
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($soulPath, $updatedSoul, $utf8NoBom)
Write-Host "Updated Hermes routing rules: $soulPath"

if ($RestartGateway) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
    if (-not $hermes) {
        throw "Hermes command not found on PATH. Restart the gateway manually."
    }
    hermes gateway restart
}

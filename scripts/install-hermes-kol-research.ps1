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
    $HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else {
        Join-Path $env:LOCALAPPDATA "hermes"
    }
}

$skillSource = Join-Path $RepoRoot "_skills\kol-research-operator\SKILL.md"
$skillTargetDir = Join-Path $HermesHome "skills\kol-research-operator"
$skillTarget = Join-Path $skillTargetDir "SKILL.md"
$operator = Join-Path $RepoRoot "scripts\hermes-kol-operator.ps1"
$pluginSource = Join-Path $RepoRoot "_automation\trading_research\hermes_operator_plugin.py"
$pluginTargetDir = Join-Path $HermesHome "plugins\kol-research-operator"
$pluginManifest = Join-Path $pluginTargetDir "plugin.yaml"
$pluginInit = Join-Path $pluginTargetDir "__init__.py"
$codexInstaller = Join-Path $RepoRoot "scripts\install-hermes-codex-delegate.ps1"
$configPath = Join-Path $HermesHome "config.yaml"

if (-not (Test-Path -LiteralPath $skillSource)) {
    throw "Required source file not found: $skillSource"
}
if (-not (Test-Path -LiteralPath $operator)) {
    throw "Required operator not found: $operator"
}
if (-not (Test-Path -LiteralPath $pluginSource)) {
    throw "Required Hermes operator plugin not found: $pluginSource"
}
if (-not (Test-Path -LiteralPath $codexInstaller)) {
    throw "Required Codex delegate installer not found: $codexInstaller"
}
$skillContent = Get-Content -LiteralPath $skillSource -Raw -Encoding UTF8
if ($skillContent.Contains("??")) {
    throw "Skill metadata contains corrupted trigger text."
}

New-Item -ItemType Directory -Force -Path $skillTargetDir | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Force
Write-Host "Installed Hermes skill: $skillTarget"

New-Item -ItemType Directory -Force -Path $pluginTargetDir | Out-Null
$pluginManifestContent = @"
name: kol-research-operator
version: 1.0.0
description: "Deterministic operator tool for the local KOL audit workbench."
author: "local"
provides_tools:
  - kol_operator
"@
$pluginInitContent = @"
from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(r"$pluginSource")


def _load_impl():
    spec = importlib.util.spec_from_file_location("ai_hub_kol_operator_plugin", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load KOL operator plugin from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:
    _load_impl().register(ctx)
"@
[System.IO.File]::WriteAllText($pluginManifest, $pluginManifestContent, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($pluginInit, $pluginInitContent, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Installed Hermes plugin: $pluginTargetDir"

& $codexInstaller -RepoRoot $RepoRoot -HermesHome $HermesHome

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Hermes config not found: $configPath"
}
$configContent = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
$pluginName = "kol-research-operator"
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
$configTempPath = "$configPath.plugin.tmp"
[System.IO.File]::WriteAllText($configTempPath, $configContent, (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $configTempPath -Destination $configPath -Force

if ($RestartGateway) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
    if (-not $hermes) {
        throw "Hermes command not found on PATH. Restart the gateway manually."
    }
    $qqPatch = Join-Path $RepoRoot "scripts\patch-hermes-qqbot-4009.ps1"
    if (Test-Path -LiteralPath $qqPatch) {
        & $qqPatch
    }
    hermes gateway restart
}

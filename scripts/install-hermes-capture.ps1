[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$HermesHome = "",
    [string]$VaultPath = "",
    [switch]$RestartGateway
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not $VaultPath) {
    $VaultPath = if ($env:OBSIDIAN_VAULT) { $env:OBSIDIAN_VAULT } else { Join-Path $RepoRoot "_automation\_vault" }
}

if (-not $HermesHome) {
    $HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }
}

$skillSource = Join-Path $RepoRoot "_skills\hermes-capture\SKILL.md"
$skillTargetDir = Join-Path $HermesHome "skills\hermes-capture"
$skillTarget = Join-Path $skillTargetDir "SKILL.md"
$pluginSource = Join-Path $RepoRoot "_automation\hermes-capture\hermes_plugin.py"
$pluginTargetDir = Join-Path $HermesHome "plugins\hermes-capture-commands"
$pluginManifest = Join-Path $pluginTargetDir "plugin.yaml"
$pluginInit = Join-Path $pluginTargetDir "__init__.py"
$configPath = Join-Path $HermesHome "config.yaml"

if (-not (Test-Path -LiteralPath $skillSource)) {
    throw "Skill source not found: $skillSource"
}
if (-not (Test-Path -LiteralPath $pluginSource)) {
    throw "Plugin source not found: $pluginSource"
}

New-Item -ItemType Directory -Force -Path $skillTargetDir | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Force
Write-Host "Installed Hermes skill: $skillTarget"

New-Item -ItemType Directory -Force -Path $pluginTargetDir | Out-Null
@"
name: hermes-capture-commands
version: 1.0.0
description: "Direct Feishu capture commands and raw-link auto capture: /auto, /clip, /idea, /readlater, /log."
author: "local"
"@ | Set-Content -LiteralPath $pluginManifest -Encoding UTF8
@"
from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(r"$pluginSource")


def _load_impl():
    spec = importlib.util.spec_from_file_location("ai_hub_hermes_capture_plugin", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Hermes capture plugin from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:
    _load_impl().register(ctx)
"@ | Set-Content -LiteralPath $pluginInit -Encoding UTF8
Write-Host "Installed Hermes plugin: $pluginTargetDir"

foreach ($dir in @("00_Inbox", "01_Sources", "02_Concepts", "03_Entities", "04_Projects", "05_Strategies", "06_Logs", "90_Archive")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $VaultPath $dir) | Out-Null
}
Write-Host "Ensured Obsidian folders under: $VaultPath"

if (-not (Test-Path -LiteralPath $configPath)) {
    New-Item -ItemType File -Force -Path $configPath | Out-Null
}

$configText = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
$oldAliasPattern = '(?ms)\r?\n?quick_commands:\s*\r?\n\s+clip:\s*\r?\n\s+type:\s*alias\s*\r?\n\s+target:\s*"/hermes-capture /clip"\s*\r?\n\s+idea:\s*\r?\n\s+type:\s*alias\s*\r?\n\s+target:\s*"/hermes-capture /idea"\s*\r?\n\s+readlater:\s*\r?\n\s+type:\s*alias\s*\r?\n\s+target:\s*"/hermes-capture /readlater"\s*\r?\n?'
$newConfigText = [regex]::Replace($configText, $oldAliasPattern, "`r`n")
if ($newConfigText -ne $configText) {
    Set-Content -LiteralPath $configPath -Value $newConfigText -Encoding UTF8
    $configText = $newConfigText
    Write-Host "Removed stale hermes-capture quick_commands aliases from: $configPath"
}

$pluginBlock = @"

plugins:
  enabled:
    - hermes-capture-commands
"@

if ($configText -notmatch "(?m)^plugins:\s*$") {
    Add-Content -LiteralPath $configPath -Value $pluginBlock -Encoding UTF8
    Write-Host "Enabled Hermes plugin in: $configPath"
} elseif ($configText -notmatch "(?m)^\s*-\s*hermes-capture-commands\s*$") {
    Write-Warning "plugins.enabled already exists in $configPath. Add hermes-capture-commands manually if missing."
} else {
    Write-Host "Hermes plugin already enabled."
}

if ($RestartGateway) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermes) {
        hermes gateway restart
    } else {
        Write-Warning "Hermes command not found on PATH. Restart the gateway manually."
    }
}

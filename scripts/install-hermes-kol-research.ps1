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
$guardSource = Join-Path $RepoRoot "scripts\hermes_ai_hub_source_guard.py"
$codexInstaller = Join-Path $RepoRoot "scripts\install-hermes-codex-delegate.ps1"
$guardTargetDir = Join-Path $HermesHome "agent-hooks"
$guardTarget = Join-Path $guardTargetDir "ai-hub-source-guard.py"
$configPath = Join-Path $HermesHome "config.yaml"
$hookAllowlistPath = Join-Path $HermesHome "shell-hooks-allowlist.json"
$hermesPython = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $skillSource)) {
    throw "Required source file not found: $skillSource"
}
if (-not (Test-Path -LiteralPath $operator)) {
    throw "Required operator not found: $operator"
}
if (-not (Test-Path -LiteralPath $pluginSource)) {
    throw "Required Hermes operator plugin not found: $pluginSource"
}
if (-not (Test-Path -LiteralPath $guardSource)) {
    throw "Required source guard not found: $guardSource"
}
if (-not (Test-Path -LiteralPath $codexInstaller)) {
    throw "Required Codex delegate installer not found: $codexInstaller"
}
if (-not (Test-Path -LiteralPath $hermesPython)) {
    throw "Hermes Python runtime not found: $hermesPython"
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

New-Item -ItemType Directory -Force -Path $guardTargetDir | Out-Null
Copy-Item -LiteralPath $guardSource -Destination $guardTarget -Force
Write-Host "Installed Hermes source guard: $guardTarget"

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Hermes config not found: $configPath"
}
$configContent = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
$legacyMatcher = "matcher: 'write_file|patch|terminal|execute_code|skill_manage'"
$currentMatcher = "matcher: 'write_file|patch|terminal|execute_code|skill_manage|delegate_task'"
if ($configContent.Contains($legacyMatcher)) {
    $configContent = $configContent.Replace($legacyMatcher, $currentMatcher)
}
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

$hookCommand = ($hermesPython.Replace('\', '/') + ' ' + $guardTarget.Replace('\', '/'))
$desiredCommandLine = "      command: '$hookCommand'"
$legacyCommandPattern = '(?m)^\s*command:\s*''[^'']*ai-hub-source-guard\.(?:ps1|cmd)''\s*$'
if ($configContent -match $legacyCommandPattern) {
    $configContent = [regex]::Replace($configContent, $legacyCommandPattern, $desiredCommandLine, 1)
    $tempPath = "$configPath.tmp"
    [System.IO.File]::WriteAllText($tempPath, $configContent, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tempPath -Destination $configPath -Force
    Write-Host "Migrated Hermes source guard to the Python hook."
} elseif (-not $configContent.Contains($guardTarget.Replace('\', '/'))) {
    if ($configContent -match '(?m)^hooks:\s*$') {
        throw "Hermes already has a hooks section. Add the ai-hub source guard without replacing existing hooks."
    }
    if ($configContent -notmatch '(?m)^hooks_auto_accept:') {
        throw "Hermes config has no hooks_auto_accept anchor. Refusing an unsafe config rewrite."
    }
    $escapedHookCommand = $hookCommand.Replace("'", "''")
    $hookBlock = @"
hooks:
  pre_tool_call:
    - matcher: 'write_file|patch|terminal|execute_code|skill_manage|delegate_task'
      command: '$escapedHookCommand'
      timeout: 5
"@
    $updatedConfig = [regex]::Replace(
        $configContent,
        '(?m)^hooks_auto_accept:',
        ($hookBlock.TrimEnd() + [Environment]::NewLine + 'hooks_auto_accept:'),
        1
    )
    $backupPath = "$configPath.before-kol-source-guard"
    Copy-Item -LiteralPath $configPath -Destination $backupPath -Force
    $tempPath = "$configPath.tmp"
    [System.IO.File]::WriteAllText($tempPath, $updatedConfig, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tempPath -Destination $configPath -Force
    Write-Host "Configured Hermes pre-tool source guard (backup: $backupPath)"
} else {
    Write-Host "Hermes pre-tool source guard is already configured."
}

$allowlist = if (Test-Path -LiteralPath $hookAllowlistPath) {
    Get-Content -LiteralPath $hookAllowlistPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    [pscustomobject]@{ approvals = @() }
}
$approvals = @($allowlist.approvals | Where-Object {
    -not ($_.event -eq "pre_tool_call" -and $_.command -eq $hookCommand)
})
$now = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ")
$mtimeCode = "from datetime import datetime, timezone; import os, sys; print(datetime.fromtimestamp(os.path.getmtime(sys.argv[1]), tz=timezone.utc).isoformat().replace('+00:00', 'Z'))"
$scriptMtime = (& $hermesPython -c $mtimeCode $guardTarget | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $scriptMtime) {
    throw "Could not compute the Hermes-compatible source guard timestamp."
}
$approvals += [pscustomobject]@{
    approved_at = $now
    command = $hookCommand
    event = "pre_tool_call"
    script_mtime_at_approval = $scriptMtime
}
$allowlistOutput = [pscustomobject]@{ approvals = $approvals } | ConvertTo-Json -Depth 8
$allowlistTempPath = "$hookAllowlistPath.tmp"
[System.IO.File]::WriteAllText($allowlistTempPath, $allowlistOutput, (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $allowlistTempPath -Destination $hookAllowlistPath -Force
Write-Host "Approved the Hermes source guard for this exact command and script version."

if ($RestartGateway) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
    if (-not $hermes) {
        throw "Hermes command not found on PATH. Restart the gateway manually."
    }
    $doctorOutput = (& hermes hooks doctor 2>&1 | Out-String).Trim()
    Write-Host $doctorOutput
    if ($LASTEXITCODE -ne 0 -or $doctorOutput -match '(?i)(issue\(s\) found|not allowlisted|modified since approval)') {
        throw "Hermes hook doctor failed."
    }
    $qqPatch = Join-Path $RepoRoot "scripts\patch-hermes-qqbot-4009.ps1"
    if (Test-Path -LiteralPath $qqPatch) {
        & $qqPatch
    }
    hermes gateway restart
}

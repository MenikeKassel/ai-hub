param(
    [string]$SourceRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$PublicRepo = $(if ($env:PUBLIC_SYSTEM_REPO) { $env:PUBLIC_SYSTEM_REPO } else { Join-Path (Split-Path $SourceRepo) 'ai-hub-public' }),
    [string]$GitProxy = $env:AI_HUB_GIT_PROXY,
    [string]$GhExe = $env:GH_EXE,
    [string]$PythonExe = $env:AI_HUB_PYTHON,
    [switch]$DryRun,
    [switch]$CreatePullRequest
)

$ErrorActionPreference = 'Stop'
if ($GitProxy) { $env:HTTP_PROXY = $GitProxy; $env:HTTPS_PROXY = $GitProxy }
$logDirectory = Join-Path $env:LOCALAPPDATA 'ai-hub-publication'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$transcriptPath = Join-Path $logDirectory "system-mirror-$runStamp-$PID.log"
Get-ChildItem -LiteralPath $logDirectory -Filter 'system-mirror-*.log' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 50 | Remove-Item -Force -ErrorAction SilentlyContinue
$transcriptStarted = $false
try { Start-Transcript -Path $transcriptPath -Append | Out-Null; $transcriptStarted = $true } catch { }
if (-not $GhExe) {
    $ghCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
    if ($ghCommand) { $GhExe = $ghCommand.Source }
}
if (-not $PythonExe) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $PythonExe = $pythonCommand.Source }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) { throw 'Python interpreter is required to build the public system mirror.' }
$mutex = New-Object System.Threading.Mutex($false, 'Global\AIHubPublicSystemMirror')
if (-not $mutex.WaitOne(0)) {
    Write-Output (@{ status = 'already_running'; public_repo = $PublicRepo } | ConvertTo-Json)
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    return
}
$mutexHeld = $true
trap {
    if ($mutexHeld) { try { $mutex.ReleaseMutex() | Out-Null } catch { } }
    $mutex.Dispose()
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch { } }
    try { $_ | Out-String | Add-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'ai-hub-publication\system-mirror-error.log') -Encoding UTF8 } catch { }
    exit 1
}
$sourceRevision = (& git -C $SourceRepo rev-parse HEAD).Trim()
if (-not $sourceRevision) { throw 'Source repository has no commit.' }
$staging = Join-Path $env:TEMP "ai-hub-public-$sourceRevision"
if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

$excludedExact = @(
    'AGENTS.md', 'CLAUDE.md', '_automation/hermes-capture/config.yaml',
    '_docs/codex-handoff.md', '_docs/github-setup.md', '_docs/hermes-qqbot-setup.md',
    '_docs/freestockdb-update-handoff-20260731.md', '_docs/purchased-daily-source.md',
    '_docs/tinyshare-source-assessment.md', '_docs/platform-reader-research.md',
    'scripts/publish-public-system.ps1', 'scripts/publish-public-data.ps1',
    'scripts/install-public-publication-tasks.ps1'
)
$excludedPrefixes = @('_runtime/', '_external/', '_data/', '.pytest_cache/', '.ruff_cache/', '_automation/trading_research/ui/e2e/console.spec.ts-snapshots/')
$safeDocs = @(
    '_docs/a-share-research-audit-v1.md',
    '_docs/instock-event-context-evaluation.md', '_docs/kol-performance-v3.md',
    '_docs/kol-research-console-v2.md', '_docs/kol-market-data-v3.md',
    '_docs/system-map.md'
)
$files = @(& git -C $SourceRepo ls-files)
foreach ($relative in $files) {
    $normalized = $relative.Replace('\', '/')
    if ($excludedExact -contains $normalized) { continue }
    if ($excludedPrefixes | Where-Object { $normalized.StartsWith($_) }) { continue }
    if ($normalized.StartsWith('_docs/') -and ($safeDocs -notcontains $normalized)) { continue }
    if ($normalized -match '(^|/)(\.env|.*secret.*|.*credential.*|.*cookie.*|.*backup.*)$') { continue }
    $destination = Join-Path $staging $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
    Copy-Item -LiteralPath (Join-Path $SourceRepo $relative) -Destination $destination -Force
}

$replacements = @{
    'C:\Users\menike' = '<USER_HOME>'
    'E:\aiworkspace' = '<AI_HUB_HOME>'
    'F:\research' = '<OBSIDIAN_VAULT>'
    'D:\ai-data' = '<MARKET_DATA_HOME>'
    'D:\a_data' = '<PURCHASED_DATA_HOME>'
    'C:/Users/menike' = '<USER_HOME>'
    'E:/aiworkspace' = '<AI_HUB_HOME>'
    'F:/research' = '<OBSIDIAN_VAULT>'
    'D:/ai-data' = '<MARKET_DATA_HOME>'
    'D:/a_data' = '<PURCHASED_DATA_HOME>'
}
$identityMap = @{}
$profileDb = Join-Path $SourceRepo '_runtime\trading\kol\posts.db'
if (Test-Path -LiteralPath $profileDb) {
    $profileCode = @'
import json, sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
db = sqlite3.connect(sys.argv[1])
rows = db.execute('select id, display_name, handle from kols order by id').fetchall()
print(json.dumps([{'id': r[0], 'display_name': r[1], 'handle': r[2]} for r in rows], ensure_ascii=True))
db.close()
'@
    $profilesJson = (& $PythonExe -c $profileCode $profileDb) -join ""
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read public KOL identity map.' }
    $profiles = $profilesJson | ConvertFrom-Json
    foreach ($profile in @($profiles)) {
        if (-not $profile.handle) { continue }
        $publicHandle = "public_kol_$($profile.id)"
        $identityMap[[string]$profile.handle] = $publicHandle
        if ($profile.display_name) { $identityMap[[string]$profile.display_name] = "Public KOL $($profile.id)" }
    }
}
Get-ChildItem -LiteralPath $staging -Recurse -File | ForEach-Object {
    if ($_.Extension -in @('.py', '.ps1', '.md', '.yaml', '.yml', '.json', '.toml', '.txt', '.csv')) {
        $content = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8
        foreach ($profile in $identityMap.GetEnumerator()) {
            $pattern = '(?i)(https?://(?:x|twitter)\.com/)' + [regex]::Escape($profile.Key) + '/status/[0-9]{10,}'
            $content = [regex]::Replace($content, $pattern, ('$1' + $profile.Value + '/status/0000000000000000000'))
        }
        foreach ($pair in $identityMap.GetEnumerator()) { $content = $content.Replace($pair.Key, $pair.Value) }
        foreach ($pair in $replacements.GetEnumerator()) {
            $content = $content.Replace($pair.Key, $pair.Value)
            $content = $content.Replace($pair.Key.Replace('\', '\\'), $pair.Value)
        }
        $content = [regex]::Replace($content, '(?i)01_Sources[/\\][^\r\n"'']+', 'source-note-placeholder.md')
        Set-Content -LiteralPath $_.FullName -Value $content -Encoding UTF8 -NoNewline
    }
}

$publicReadme = @'
# ai-hub

Personal research-system orchestration reference for evidence-first KOL and
A-share audit workflows. This mirror contains source code, schemas, tests,
generic task scripts, and documentation. It contains no operator runtime,
credentials, private notes, social-media snapshots, media, or market database.

Configure local paths with `AI_HUB_HOME`, `KOL_DATA_HOME`, `OBSIDIAN_VAULT`,
and `HERMES_HOME`. Data publication is handled by the separate
`kol-audit-dataset` repository. The reusable cross-platform core is
[`kol-audit-workbench`](https://github.com/MenikeKassel/kol-audit-workbench).

Read [RIGHTS.md](RIGHTS.md), [SECURITY.md](SECURITY.md), and
[DISCLAIMER.md](DISCLAIMER.md) before using the adapters.
'@
Set-Content -LiteralPath (Join-Path $staging 'README.md') -Value $publicReadme -Encoding UTF8
$sourceInfo = @{ schema_version = 'public-system-v1'; source_revision = $sourceRevision; generated_by = 'publish-public-system.ps1' } |
    ConvertTo-Json -Depth 4
Set-Content -LiteralPath (Join-Path $staging 'PUBLIC_SOURCE.json') -Value $sourceInfo -Encoding UTF8

$scanTargets = Get-ChildItem -LiteralPath $staging -Recurse -File
$forbiddenFiles = @($scanTargets | Where-Object {
    $_.Length -gt 50MB -or $_.Extension -match '(?i)^\.(db|sqlite|duckdb|parquet|png|jpg|jpeg|gif|webp|mp4|mov|avi)$'
})
if ($forbiddenFiles.Count -gt 0) { throw "Public mirror contains forbidden runtime/media files: $($forbiddenFiles.Count)." }
$pathFindings = @($scanTargets | Select-String -Pattern '(?i)auth_token\s*[:=]\s*["''][A-Za-z0-9._-]{20,}["'']|ct0\s*[:=]\s*["''][A-F0-9]{32,}["'']|(?:api[_ -]?key|secret|password)\s*[:=]\s*["''][^"'']{16,}["'']|sk-[A-Za-z0-9_-]{20,}|-----BEGIN [^-]+ KEY-----|C:\\+Users\\+menike|E:\\+aiworkspace|F:\\+research|D:\\+ai-data|D:\\+a_data' -AllMatches)
if ($pathFindings.Count -gt 0) { throw "Public mirror scan found forbidden content in $($pathFindings.Count) lines." }

if ($DryRun) {
    Write-Output (@{ status = 'dry_run'; source_revision = $sourceRevision; files = $files.Count; identity_count = $identityMap.Count; staging = $staging } | ConvertTo-Json)
    if ($mutexHeld) { $mutex.ReleaseMutex() | Out-Null; $mutexHeld = $false }
    $mutex.Dispose()
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    return
}
if (-not $CreatePullRequest) { throw 'Public system publication requires -CreatePullRequest; direct pushes are disabled.' }

if (-not (Test-Path -LiteralPath $PublicRepo)) { New-Item -ItemType Directory -Force -Path $PublicRepo | Out-Null }
if (-not (Test-Path -LiteralPath (Join-Path $PublicRepo '.git'))) {
    & git -C $PublicRepo init --initial-branch=main | Out-Host
    & git -C $PublicRepo remote add origin 'https://github.com/MenikeKassel/ai-hub.git'
} else {
    & git -C $PublicRepo fetch origin | Out-Host
}
Get-ChildItem -LiteralPath $PublicRepo -Force | Where-Object { $_.Name -notin @('.git') } | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $staging '*') -Destination $PublicRepo -Recurse -Force
& git -C $PublicRepo add -A
$stagedFiles = @(& git -C $PublicRepo diff --cached --name-only)
if ($stagedFiles.Count -gt 0) {
    if (-not $GhExe -or -not (Test-Path -LiteralPath $GhExe)) { throw 'GitHub CLI (gh.exe) is required to create the public mirror pull request.' }
    & git -C $PublicRepo -c user.name='ai-hub Mirror Bot' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' commit -m "chore: publish sanitized system $sourceRevision" | Out-Host
    $branch = "public-sync/$($sourceRevision.Substring(0, 12))"
    & git -C $PublicRepo branch -M $branch
    & git -C $PublicRepo push --set-upstream origin $branch | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Public system push failed; local mirror was not reported as published.' }
    $existing = (& $GhExe pr list --repo MenikeKassel/ai-hub --head $branch --json number --jq 'length').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI could not query existing public mirror pull requests.' }
    if ($existing -eq '0') { & $GhExe pr create --repo MenikeKassel/ai-hub --base main --head $branch --title "Publish sanitized system $($sourceRevision.Substring(0, 12))" --body 'Automated sanitized mirror. Merge only after security scan and tests.' | Out-Host; if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI could not create the public mirror pull request.' } }
}
if ($mutexHeld) { $mutex.ReleaseMutex() | Out-Null; $mutexHeld = $false }
$mutex.Dispose()
Write-Output (@{ status = 'published'; source_revision = $sourceRevision; repo = $PublicRepo } | ConvertTo-Json)
if ($transcriptStarted) { Stop-Transcript | Out-Null }

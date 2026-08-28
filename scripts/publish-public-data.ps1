param(
    [string]$SourceRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$DataRepo = $(if ($env:PUBLIC_DATA_REPO) { $env:PUBLIC_DATA_REPO } else { Join-Path (Split-Path $SourceRepo) 'kol-audit-dataset' }),
    [string]$GitProxy = $env:AI_HUB_GIT_PROXY,
    [string]$GhExe = $env:GH_EXE,
    [string]$PythonExe = $env:AI_HUB_PYTHON,
    [switch]$DryRun,
    [switch]$CreateRelease
)

$ErrorActionPreference = 'Stop'
if ($GitProxy) { $env:HTTP_PROXY = $GitProxy; $env:HTTPS_PROXY = $GitProxy }
if (-not $PythonExe) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $PythonExe = $pythonCommand.Source }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) { throw 'Python interpreter is required to export the public dataset.' }
if ($CreateRelease -and -not $GhExe) {
    $ghCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
    if ($ghCommand) { $GhExe = $ghCommand.Source }
}
if ($CreateRelease -and (-not $GhExe -or -not (Test-Path -LiteralPath $GhExe))) {
    throw 'GitHub CLI (gh.exe) is required to create a public data release.'
}
$mutex = New-Object System.Threading.Mutex($false, 'Global\AIHubPublicDatasetPublication')
if (-not $mutex.WaitOne(0)) {
    Write-Output (@{ status = 'already_running'; data_repo = $DataRepo } | ConvertTo-Json)
    return
}
$mutexHeld = $true
$logDirectory = Join-Path $env:LOCALAPPDATA 'ai-hub-publication'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$transcriptPath = Join-Path $logDirectory "data-publish-$runStamp-$PID.log"
Get-ChildItem -LiteralPath $logDirectory -Filter 'data-publish-*.log' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 50 | Remove-Item -Force -ErrorAction SilentlyContinue
$transcriptStarted = $false
try { Start-Transcript -Path $transcriptPath -Append | Out-Null; $transcriptStarted = $true } catch { }
trap {
    if ($mutexHeld) { try { $mutex.ReleaseMutex() | Out-Null } catch { } }
    $mutex.Dispose()
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch { } }
    try { $_ | Out-String | Add-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'ai-hub-publication\data-publish-error.log') -Encoding UTF8 } catch { }
    exit 1
}
$cli = Join-Path $SourceRepo '_automation\trading_research\trading_cli.py'
if (-not (Test-Path -LiteralPath $cli)) { throw "Trading CLI not found: $cli" }
if (-not (Test-Path -LiteralPath $DataRepo)) { New-Item -ItemType Directory -Force -Path $DataRepo | Out-Null }
if (-not (Test-Path -LiteralPath (Join-Path $DataRepo '.git'))) {
    & git -C $DataRepo init --initial-branch=main | Out-Host
    & git -C $DataRepo remote add origin 'https://github.com/MenikeKassel/kol-audit-dataset.git'
}
$latest = Join-Path $DataRepo 'latest'
& $PythonExe $cli public-dataset-export --output $latest | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Public dataset export failed; previous published snapshot is retained.' }
& $PythonExe $cli public-dataset-validate --input $latest | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Public dataset validation failed; previous published snapshot is retained.' }
$readme = @'
# kol-audit-dataset

Sanitized, read-only snapshots from the KOL audit workbench. Start with
[`GPT_CONTEXT.md`](latest/GPT_CONTEXT.md) and the dictionary files in `latest/`.

This repository contains derived audit facts and short redacted evidence only.
It does not contain full post text, media, credentials, local databases,
private notes, personal transactions, or raw/purchased market data.
'@
Set-Content -LiteralPath (Join-Path $DataRepo 'README.md') -Value $readme -Encoding UTF8
$rights = @'
# Rights

This repository contains a public, derived snapshot of a private research
system. No license is granted. All rights are reserved by the repository
owner unless a separate written agreement says otherwise.

Public source links and short evidence excerpts remain subject to the terms
and rights of their respective platforms and authors.
'@
Set-Content -LiteralPath (Join-Path $DataRepo 'RIGHTS.md') -Value $rights -Encoding UTF8
$security = @'
# Security

Do not add credentials, cookies, request headers, local databases, media,
private notes, personal transactions, or raw/purchased market data.
'@
Set-Content -LiteralPath (Join-Path $DataRepo 'SECURITY.md') -Value $security -Encoding UTF8
$disclaimer = @'
# Disclaimer

This dataset is an evidence and performance-audit research artifact. It is
not investment advice, a recommendation, or an instruction to trade.
'@
Set-Content -LiteralPath (Join-Path $DataRepo 'DISCLAIMER.md') -Value $disclaimer -Encoding UTF8
$gitignore = @'
_staging/
*.db
*.duckdb
*.parquet
raw/
media/
*.zip
'@
Set-Content -LiteralPath (Join-Path $DataRepo '.gitignore') -Value $gitignore -Encoding UTF8
& git -C $DataRepo add -A
$changed = @(& git -C $DataRepo diff --cached --name-only)
if ($DryRun) {
    Write-Output (@{ status = 'dry_run'; changed_files = @($changed).Count; data_repo = $DataRepo } | ConvertTo-Json)
    return
}
$ahead = 0
$aheadText = @(& git -C $DataRepo rev-list --left-right --count origin/main...HEAD 2>$null)
if ($LASTEXITCODE -eq 0 -and $aheadText.Count -gt 0) {
    $parts = $aheadText[0].ToString().Trim() -split '\s+'
    if ($parts.Count -gt 1) { $ahead = [int]$parts[1] }
}
$shouldPush = @($changed).Count -gt 0 -or $ahead -gt 0
if ($shouldPush) {
    $asOf = (Get-Date).ToString('yyyy-MM-dd')
    if (@($changed).Count -gt 0) {
        & git -C $DataRepo -c user.name='kol-audit Dataset Bot' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' commit -m "data: publish sanitized snapshot $asOf" | Out-Host
    }
    & git -C $DataRepo push --set-upstream origin main | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Public data push failed; local dataset was not reported as published.' }
}
if ($CreateRelease) {
    $asOf = (Get-Date).ToString('yyyy-MM-dd')
    $tag = "snapshot-$($asOf.Replace('-', '.'))"
    $releaseTags = @(& $GhExe release list --repo MenikeKassel/kol-audit-dataset --limit 100 --json tagName --jq '.[].tagName')
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI could not query public data releases.' }
    if ($releaseTags -notcontains $tag) {
        $zip = Join-Path $env:TEMP "kol-audit-dataset-$asOf.zip"
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
        Compress-Archive -Path (Join-Path $latest '*') -DestinationPath $zip -CompressionLevel Optimal
        if ((Get-Item -LiteralPath $zip).Length -ge 1.8GB) { throw 'Release asset is too large.' }
        & $GhExe release create $tag $zip --repo MenikeKassel/kol-audit-dataset --title "Dataset $asOf" --notes 'Sanitized derived snapshot. See latest manifest and dictionaries.' | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Public data release creation failed.' }
        $releaseStatus = 'created'
    } else {
        $releaseStatus = 'already_exists'
    }
} else {
    $releaseStatus = 'not_requested'
}
if ($mutexHeld) { $mutex.ReleaseMutex() | Out-Null; $mutexHeld = $false }
$mutex.Dispose()
if ($transcriptStarted) { Stop-Transcript | Out-Null }
Write-Output (@{ status = 'published'; changed_files = @($changed).Count; release = $releaseStatus; data_repo = $DataRepo } | ConvertTo-Json)

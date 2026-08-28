param(
    [switch]$SkipHermes,
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
$vaultPath = if ($env:OBSIDIAN_VAULT) { $env:OBSIDIAN_VAULT } else { Join-Path $Repo "_automation\_vault" }

function Section($Name) {
    Write-Host ""
    Write-Host "== $Name ==" -ForegroundColor Cyan
}

Section "Workspace"
Write-Host "Repo: $Repo"
Write-Host "Obsidian vault: $vaultPath"
Write-Host "Hermes runtime: $env:LOCALAPPDATA\hermes"
Write-Host "Hermes user config: $env:USERPROFILE\.hermes"

Section "Git"
git -C $Repo status --short
git -C $Repo remote -v

Section "Startup Docs"
$docs = @(
    "CLAUDE.md",
    "AGENTS.md",
    "_docs\a-share-research-audit-v1.md",
    "_docs\a-share-implementation-plan-v1.md",
    "_docs\kol-market-data-v3.md",
    "_docs\codex-handoff.md"
)
foreach ($doc in $docs) {
    $path = Join-Path $Repo $doc
    if (Test-Path $path) {
        Write-Host "[OK] $doc"
    } else {
        Write-Host "[MISSING] $doc" -ForegroundColor Yellow
    }
}

if (-not $SkipHermes) {
    Section "Hermes Gateway"
    hermes gateway status

    Section "Hermes Capture Doctor"
    python (Join-Path $Repo "_automation\hermes-capture\hermes_capture_doctor.py")
}

if (-not $SkipTests) {
    Section "Capture Parse Tests"
    python (Join-Path $Repo "_automation\hermes-capture\tests\test_parse.py")

    Section "Trading Research Doctor"
    $tradingPython = Join-Path $Repo "_runtime\venv-trading\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $tradingPython)) { $tradingPython = "python" }
    $tradingCli = Join-Path $Repo "_automation\trading_research\trading_cli.py"
    & $tradingPython $tradingCli doctor

    Section "KOL Collection Doctor"
    & $tradingPython $tradingCli kol-post-doctor

    Section "Market Data Doctor"
    & $tradingPython $tradingCli market-doctor

    Section "Research Console Doctor"
    & $tradingPython $tradingCli kol-ui-doctor
}

Section "Done"
Write-Host "Read CLAUDE.md first when resuming with Claude."
Write-Host "Read AGENTS.md first when resuming with Codex."

# ai-hub

Personal AI workspace source repository.

Current source version: **3.0.0**. The KOL research console is consolidated at
`http://127.0.0.1:8123/#/kols`; market data is historical and read-only through
`2026-08-25`.

For code review, start with:

- `_docs/glm-review.md`
- `_docs/architecture-current.md`
- `_docs/operations-current.md`
- `CHANGELOG.md`

This repo stores automation code, Hermes skills/plugins, configuration
templates, runbooks, and system docs. It must not store Notion exports,
Obsidian vault content, API keys, Feishu secrets, `_runtime`, or `_external`.

## Environment

Set the `AI_HUB_HOME` environment variable to the repository root; all
commands below (and the automation code) resolve paths from it:

```powershell
$env:AI_HUB_HOME = "C:\path\to\ai-hub"   # 示例:指向仓库根,按实际位置修改
```

## Current Mainline

```text
X / Zhihu public evidence
-> durable local post collection
-> rules / OCR / bounded model classification
-> human review and formal event audit
-> historical market context and return display
```

Hermes capture remains a separate optional workflow. Its real configuration is
local-only; Git contains `config.example.yaml`.

## Current v1 Focus

A-share beginner loss-prevention and research audit system.

Confirmed boundary:

- Not an automatic trading system.
- Not a stock recommendation engine.
- Not KOL follow-trading.
- Not full-market quant infrastructure.

Read the blueprint:

- `CLAUDE.md`
- `AGENTS.md`
- `_docs/a-share-research-audit-v1.md`
- `_docs/a-share-implementation-plan-v1.md`
- `_docs/kol-market-data-v3.md`

## Directory Map

```text
_automation/
  codex-delegate/          # Hermes -> local Codex execution bridge
  hermes-capture/          # Feishu/Hermes -> Notion/Obsidian capture pipeline
  trading_research/        # read-only A-share research helper
_docs/                     # system docs and runbooks
_skills/                   # canonical, sanitized Hermes skill sources
_templates/                # config and Obsidian templates
scripts/                   # install and doctor scripts
```

Ignored:

```text
_external/
_runtime/
```

## Resume With Claude Or Codex

Claude should read `CLAUDE.md` first.

Codex should read `AGENTS.md` first.

Then read `_docs/a-share-implementation-plan-v1.md` and run:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:AI_HUB_HOME\scripts\doctor.ps1"
```

## Install Hermes Capture

```powershell
powershell -ExecutionPolicy Bypass -File "$env:AI_HUB_HOME\scripts\install-hermes-capture.ps1" -RestartGateway
```

## Local Tests

```powershell
python "$env:AI_HUB_HOME\_automation\hermes-capture\hermes_capture_doctor.py"
python "$env:AI_HUB_HOME\_automation\hermes-capture\tests\test_parse.py"
python "$env:AI_HUB_HOME\_automation\trading_research\trading_cli.py" doctor
```

Start the local KOL research console:

```powershell
& "$env:AI_HUB_HOME\scripts\start-kol-ui.ps1"
python "$env:AI_HUB_HOME\_automation\trading_research\trading_cli.py" kol-post-doctor
python "$env:AI_HUB_HOME\_automation\trading_research\trading_cli.py" kol-fallback-mode
python "$env:AI_HUB_HOME\_automation\trading_research\trading_cli.py" market-doctor
```

The KOL console and discovery UI share `http://127.0.0.1:8123`; legacy port 8765
must stay stopped. The local Nitter shadow service uses 9377. See the current
architecture and operations docs before changing provider mode or credentials.

Install the KOL-to-stock and market data tasks:

```powershell
& "$env:AI_HUB_HOME\scripts\install-kol-recovery-tasks.ps1"
```

Historical mode intentionally does not install market update, return tracking,
or research recomputation tasks.

The console now includes `Stock Leads` and `Market Data`. KOL posts, reviews,
approved events, returns, and market data stay local and do not enter Notion or
Obsidian. General Hermes knowledge capture remains a separate workflow.

The board-mainline subsystem was removed in 2026-08; the local market store
still keeps board tables only as read-only context for the multi-method event
research leader lens.

## GitHub Remote

If this source is pushed, use a **private** remote and run the review-bundle
check first. No remote is configured automatically by local setup.

Expected remote:

```text
https://github.com/MenikeKassel/ai-hub.git
```

Push:

```powershell
git -C "$env:AI_HUB_HOME" push -u origin main
```

If GitHub connectivity fails, retry after proxy/network is stable.

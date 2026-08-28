# AGENTS.md

This is the Codex entrypoint for `D:\aiworkspace\ai-hub`.

Read `CLAUDE.md` first. Then read `_docs/a-share-research-audit-v1.md` and
`_docs/a-share-implementation-plan-v1.md` before making changes to trading,
Hermes, Notion, or Obsidian workflows.

## Current Focus

The active v1 is an A-share beginner loss-prevention and research audit system.
It is not an automatic trading system, stock recommendation engine, KOL copy
trading system, or full quant platform.

## Non-Negotiable Boundaries

- Notion stores raw material and workflow status.
- Obsidian stores processed judgments, LLM Wiki pages, stock audit cards, and reviews.
- The KOL research console is a local-only subsystem: posts, media, reviews,
  events, returns, and generated reports stay under `_runtime\trading` and do
  not write Notion or Obsidian.
- Hermes can capture and classify, but must not promote leads or make buy/sell calls.
- Git stores only code, docs, templates, and scripts.
- Do not commit secrets, Notion exports, `_runtime`, `_external`, or the full Obsidian vault.
- Do not expand scope into HIL learning, math modeling, life notes, or "mom index" unless the user explicitly reopens those tracks.

## Implementation Preference

Keep v1 small:

- `/clip`, `/idea`, `/readlater`, `/auto`, and `/log` remain the general capture commands.
- `/kol` records a KOL profile/source lead.
- `/event` records a KOL recommendation candidate, but does not promote it into the formal event table.
- `/concept` records a reusable trading/research concept lead.
- `/holding` records a real holding or position audit lead.
- No `/promote` command in v1.
- Do not assume older planned `/stock` or `/impulse` commands exist unless they are reintroduced in code.
- KOL workflow state stays in SQLite. Market facts use immutable raw snapshots,
  normalized Parquet, DuckDB metadata/views, manifests, and quality audits.
- Human approval in the KOL console registers a local return event directly;
  it must not call the Hermes capture pipeline.
- Daily data remains primary. Minute data is an optional local event-dossier
  context window only; it is not part of daily sync, ranking, or signal logic.

## Hermes To Codex Routing

- Hermes handles conversation and the established capture commands.
- Complex implementation, debugging, multi-file changes, Git work, Obsidian
  restructuring, and system integration must try the `delegate-to-codex` skill
  before `claude-code` or Hermes's own execution tools.
- Use `claude-code` only when the user explicitly asks for Claude, or after a
  Codex failure and user-approved fallback.
- High-impact or irreversible actions still require explicit user confirmation.
- Install or refresh the runtime rule with
  `scripts\install-hermes-codex-delegate.ps1`.

## Useful Commands

```powershell
python D:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_capture_doctor.py
python D:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_parse.py
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py doctor
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py kol-post-doctor
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py kol-fallback-mode
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py market-doctor
```

KOL console runbook: `_docs/kol-research-console-v2.md`. The local UI is
`http://127.0.0.1:8123`; Nitter is loopback-only at
`http://127.0.0.1:9377`. Keep fallback in `shadow` until the CLI reports that
the three-day 95% coverage gate is ready.

KOL-to-stock collection and market data runbook:
`_docs/kol-market-data-v3.md`. Do not process the paused 62.9 GB minute dataset
or commit anything under `_runtime`.

If Hermes plugin code changes, restart the gateway:

```powershell
hermes gateway --accept-hooks restart
```

# CLAUDE.md

This repository is the source-of-truth for the local AI research workspace.
Use this file as the first read when Claude starts in `D:\aiworkspace\ai-hub`.

## Mission

Build a personal research system that turns external information into
traceable, auditable, reviewable research judgments.

Do not optimize for saving everything. Do not build an automatic trading
system. The current v1 focus is an A-share beginner loss-prevention and
research audit system.

## Tool Boundaries

- Feishu: fast input.
- Hermes: capture, classify, mark status, ask for missing source material.
- Notion: raw material database and state machine.
- The former F-drive Obsidian vault is legacy history, not a current runtime dependency.
- `D:\aiworkspace\ai-hub`: automation code, docs, scripts, and runbooks.
- Legacy E-drive quant/backtest sandboxes are reference-only and outside this project.

The KOL research console is an explicit exception to the general capture
mainline. Its posts, media, review ledger, formal events, return marks, and
reports remain local under `_runtime\trading`; approving a KOL event must not
create Notion pages or Obsidian notes.

## Current Mainline

```text
Feishu input
-> Hermes capture
-> Notion raw record and state
-> manual or semi-automatic review
-> Obsidian LLM Wiki only for processed knowledge
-> investment research review
```

## A-Share v1 Rules

- Research method: falsification-first fundamental audit.
- Feature layer: simple factors and technical indicators such as RSI, MA, volume,
  turnover, volatility, and relative strength.
- Funds flow and sentiment: weak state features only.
- KOL opinions: leads only, never follow-trade signals.
- Backtesting: validation tool only.
- Output: stock audit card, not buy/sell advice.

## Stock Funnel

```text
Unlimited leads
-> candidate watch pool, max 20
-> deep research pool, max 5
-> buy candidate can be 0
```

Upgrade from lead to candidate requires at least 2 upgrade conditions.
Upgrade from candidate to deep research requires 3 positive evidence items,
1 disconfirming/risk item, and 1 explicit research question.

## Hermes v1 Commands

- `/clip <url> [note]`: capture external material to Notion and Obsidian Inbox.
- `/idea <text>`: capture a thought to Notion and Obsidian Inbox.
- `/readlater <url> [note]`: save to Notion only.
- `/auto <shared text>`: URL text becomes `/clip`; plain text becomes `/log`.
- `/log <text>`: daily activity log to Notion only.
- `/kol <url> [note]`: capture a KOL profile/source lead.
- `/event <url> [note]`: capture a KOL recommendation candidate; formal event table entry still requires six-element audit.
- `/concept <text or url> [note]`: capture a reusable trading/research concept lead.
- `/holding <text>`: capture a real holding or position audit lead.

Do not implement `/promote` in v1. Promotion is a manual audit action.

## Hermes To Codex Routing

Hermes should delegate complex execution to the local Codex CLI through the
`delegate-to-codex` skill. This includes code changes, debugging, multi-file or
batch operations, Git work, Obsidian restructuring, and system integration.
Try `delegate-to-codex` before `claude-code` or Hermes's own execution tools;
use Claude only when the user explicitly asks for it or approves fallback after
Codex fails.
Conversation and the capture commands stay native to Hermes. Deletion, external
publishing, credential operations, trading, and other high-impact actions require
the user's explicit confirmation before delegation.

## Data Layer v3

The shared A-share daily foundation is the read-only truth source for securities,
the trading calendar, raw/qfq daily bars, and common daily features:

```text
F:\ai-data\ashare\current.json
F:\ai-data\ashare\warehouse
F:\ai-data\ashare\features
F:\ai-data\ashare\catalogs
```

The KOL runtime may retain only selected-instrument workflow state and optional
minute/event-dossier context under `_runtime/trading/market`; it must not bulk-copy
the foundation's instrument catalog, calendar, or daily bars. BaoStock is the
foundation's core daily source and FreeStockDB provides factor enrichment,
cross-checking, and explicit fallback. Provider failure must not overwrite a
passing published dataset.

The daily data layer does not fetch minute data, Level2, automatic sentiment
indices, full-market daily factor scans, or trading interfaces. A formal KOL
event dossier may read optional, already-collected local minute snapshots as a
clearly labelled post-event context window; it does not fetch them in daily
sync and they never create trading signals. The 62.9 GB minute dataset remains
paused and immutable.

## Key Docs

- `_docs/a-share-research-audit-v1.md`
- `_docs/a-share-implementation-plan-v1.md`
- `_docs/codex-handoff.md`
- `_docs/hermes-obsidian-optimization-plan.md`
- `_docs/notion-obsidian-schema.md`
- `_docs/kol-research-console-v2.md`
- `_docs/kol-market-data-v3.md`

## Startup Checklist

```powershell
git status --short
python D:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_capture_doctor.py
python D:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_parse.py
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py doctor
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py kol-post-doctor
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py kol-fallback-mode
python D:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py market-doctor
```

The KOL console runs at `http://127.0.0.1:8123`. X collection starts in
`shadow` mode. Do not enable Nitter fallback until `kol-fallback-mode` reports
three qualifying comparison days. Credentials are local runtime state and must
never be requested in chat or committed.

Keep secrets, Notion exports, Obsidian vault content, `_external`, and `_runtime`
out of git.

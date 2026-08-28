# KOL and A-share Data Collection v3

Updated: 2026-07-14

## Purpose

This layer collects evidence. It does not rank KOLs, recommend stocks, size
positions, or place trades.

```text
X accounts -> local posts/media -> rules -> Unlimited-OCR -> Codex if ambiguous
-> unified human review -> stock lead + formal event -> market/return refresh
```

Raw posts, reviewed KOL events, return marks, and market datasets stay local.
The KOL console does not create or update Notion pages or Obsidian notes.
General knowledge capture remains a separate Hermes workflow.

## Runtime

- Console: `http://127.0.0.1:8123`
- KOL SQLite: `_runtime\trading\kol\posts.db`, schema version 10
- Market root: `_runtime\trading\market`
- Market catalog: `_runtime\trading\market\market.duckdb`
- Daily facts: `_runtime\trading\market\warehouse\daily`
- Immutable provider snapshots: `_runtime\trading\market\raw`
- Manifests and audits: `_runtime\trading\market\manifests` and `audits`

Git excludes all runtime databases, media, snapshots, Parquet, credentials, and
logs.

## Unified review contract

- The normal workflow has three visible stages: collection, review, and return audit.
- Approving a post confirms the draft symbols against the instrument master,
  persists the stock leads, creates one formal event per symbol, and queues an
  immediate market/return refresh.
- A failed refresh never removes the approved event. The durable market queue
  and next scheduled run recover it.
- The stock-lead page remains an audit archive for corrections and exceptional
  cases, not a mandatory step before every approval.

## Stock lead contract

- One post can produce multiple stock leads.
- Exact six-digit codes are auto-confirmed only after matching the instrument
  master. This grants data collection only.
- Name-only and ambiguous results remain pending until the unified review.
- Themes and methods are not forced into a stock.
- Recommendation, retrospective, secondhand, holding, and analysis mentions
  remain distinct.
- Post review status and formal KOL event status are never changed by lead
  extraction.

The lead registry has no business count limit. Extraction runs in batches and
records a source signature so interrupted work is resumable and idempotent.

Each KOL also has an independent durable backfill request. The KOL management
page can queue 200 historical posts; the next 19:00 run asks twitter-cli, whose
timeline implementation follows X bottom cursors. The local queue advances in
durable cumulative pages of 100 posts (`100 -> 200 -> target`), deduplicating
the overlap. An interrupted page restarts without losing the last completed
depth. A failed or gap-detected run stays queued. A short successful response
becomes `needs_review` instead of being silently treated as complete; the UI
shows the returned/requested counts and permits an explicit retry.

## Market contract

Instrument lifecycle:

- `pinned`: holdings, manual watchlist, benchmarks, and active formal events.
- `tracking`: confirmed KOL leads, retained for at least 120 open sessions.
- `archived`: retained in the catalog but excluded from daily sync.

The full provider contract catalog uses `exchange:symbol:type` identity, so an
index and stock may legally share six digits. The active research registry keeps
one preferred contract per symbol; pinned/tracking entries always win over a
master refresh. Raw and Parquet master snapshots retain every contract.

BaoStock supplies the normal daily series and trading calendar. AKShare is a
best-effort secondary source for cross-checks and weekly research snapshots.
Fund-flow snapshots always retain the provider and an experimental label.

Quality errors quarantine a batch. The raw snapshot and audit remain, while the
last passing Parquet dataset is left untouched. Daily writes overlap the last
10 calendar days to capture corrections and are deduplicated by symbol, date,
and adjustment.

## Commands

```powershell
python trading_cli.py kol-leads-extract --pending
python trading_cli.py market-init
python trading_cli.py market-doctor
python trading_cli.py market-backfill --symbols "600900,159139,000300" --start 2024-01-01
python trading_cli.py market-sync --as-of 2026-07-14
python trading_cli.py market-audit --symbols "600900,159139,000300" --cross-check
python trading_cli.py market-weekly --as-of 2026-07-14
python trading_cli.py kol-context-doctor
python trading_cli.py kol-context-backfill --all
```

`market-sync` and `market-backfill` compute pending event-time technical context
after normalized QFQ data is available. Event baselines and official returns
continue to use raw prices. Missing history is stored as a visible partial state,
never zero-filled.

Market and context CLI commands must run serially across processes. The normal
scheduled pipeline already enforces this order and uses the shared DuckDB lock.

Quote comma-separated symbols in PowerShell so leading zeroes such as `000300`
are preserved.

## Scheduled tasks

- `KOL_Post_Fetch_Daily`: 19:00, fetch and transparent rules.
- `Market_Data_Sync_Daily`: weekdays 19:30.
- `KOL_Return_Tracker_Daily`: weekdays 20:00.
- `KOL_Post_Classify_Daily`: 20:20, local OCR first and durable sequential Codex fallback.
- `Research_Data_Digest_Daily`: 20:40, one normal Feishu summary.
- `Market_Data_Weekly`: Sunday 10:00.

All tasks use `StartWhenAvailable`, `IgnoreNew`, a script mutex, and local
rolling logs. AKShare weekly calls run in isolated child processes with a
45-second timeout, so one endpoint cannot block the entire refresh.

OCR status and text are stored separately from the original post. OCR output is
evidence for a second rule pass, never a recommendation decision. Clear symbol
and direction matches are marked `not_needed` for Codex. Remaining Codex
candidates are claimed atomically in SQLite one at a time. Interrupted
claims return to the queue after ten minutes. Failed classifications retry after
six hours, up to three attempts, and then remain visible for manual repair. A
database-scoped process lock guarantees that the API and scheduled CLI cannot
run separate Codex workers concurrently.

On Windows the worker resolves the npm `codex.cmd` shim but invokes its bundled
JavaScript through Node directly, so the process cannot outlive the temporary
JSON output directory. It also uses `--ignore-user-config`; unrelated MCP
authentication failures must not block post classification.

## Recovery

```powershell
python trading_cli.py kol-post-doctor
python trading_cli.py market-doctor
python trading_cli.py market-audit
```

Do not enable Nitter fallback until the existing three-day, 95% shadow-coverage
gate passes. Do not activate the paused minute dataset in this phase.

X session values that were pasted into chat must be rotated before treating the
collector as production-ready. Store replacement values only through the local
System page and Windows Credential Manager.

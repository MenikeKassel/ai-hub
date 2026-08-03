---
name: kol-research-operator
description: Operate the local KOL audit workbench when the user asks to start or open the KOL research desk, collect today's posts, run AI review, inspect data health, manage KOL accounts, review recommendation drafts, update market data and returns, or inspect the board RPS mainline module. Use this skill for Chinese or English requests about the KOL audit console. All routine start and collection operations are Codex-independent.
---

# KOL Research Operator

Use only the native `kol_operator` tool. It calls the deterministic Windows operator without routing through Git Bash, WSL, or the general-purpose terminal.

The console URL is `http://127.0.0.1:8123`.

## Hard Boundaries

- Hermes is the workbench operator, never its source-code maintainer.
- Never use the `terminal` or `read_file` tool to operate or diagnose this workbench. Call `kol_operator` once with a documented action below.
- Never use `write_file`, `patch`, `execute_code`, raw PowerShell, raw Python, Git, SQL, or file-management commands against `<AI_HUB_HOME>\ai-hub`.
- Any request to inspect, explain, repair, refactor, configure, test, commit, or otherwise change source code, schemas, scripts, scheduled tasks, or operator policy must use the `delegate-to-codex` skill.
- If Codex delegation is unavailable or out of quota, report the blocked maintenance request and preserve the current state. Never attempt a Hermes fallback edit.
- Never run uvicorn, Python modules, database commands, or raw fetch commands.
- Never wait for collection, AI review, market sync, or returns in the foreground.
- Never call Codex merely to start, open, collect, inspect, or manage the console.
- Treat collection and AI review as separate operations. Collection must survive Codex quota exhaustion.
- Never approve or reject without an explicit draft ID from the user.
- Never change a KOL or event without an explicit ID and requested action.
- `triggered` means accepted for background execution, not completed.
- If AI is blocked, report that posts are preserved and manual review remains available.

## Service And Health

- Status: `kol_operator({"action":"status"})`.
- Start without opening a browser: `kol_operator({"action":"start"})`.
- Start and open the browser: `kol_operator({"action":"open"})`.
- Detailed health and task state: `kol_operator({"action":"doctor"})`.

Run status before start. Do not start a second server when `running=true`.

The start state machine is fixed:

```text
status -> running=true: report the existing URL
status -> running=false: call start once
start -> started/already_running: report the URL
start -> startup_failed/port_conflict/unhealthy: report the exact operator error and stop
```

After a start error, never use `terminal`, `read_file`, raw PowerShell, raw
Python, a browser workaround, or a guessed alternate port. Do not retry the
start in the same turn. Source, environment, database-lock, scheduled-task,
or policy problems must be handed to `delegate-to-codex`; preserve the current
state and show the returned `error`, `error_log`, and `stderr_tail` when present.

`running=true` means the health endpoint is ready. A listener without a ready
health endpoint is `unhealthy`, not a reason to launch another server.

## Data Tasks

- Collect posts without AI: action `collect`.
- Run optional AI prefill: action `review`.
- Sync market data: action `market`.
- Update KOL returns: action `returns`.
- Inspect board RPS health: action `board-status`.
- Trigger the board snapshot/RPS task: action `board-sync`.

`collect` is always Codex-independent. If `review` returns `blocked`, do not retry unless the user explicitly asks to force one attempt; then add `-Force` once.

## KOL Management

- List: action `list-kols`.
- Add X: action `add-kol` with `handle`, `display_name`, `platform`, and `profile_url`.
- Add Zhihu: use the same command with `-Platform Zhihu`.
- Pause or resume: action `set-kol-status` with explicit `id` and `status`.

Do not infer a display name, platform, profile URL, or ID when it is absent.

## Draft Review

- List today's drafts: action `list-drafts`.
- List another date: action `list-drafts` with `review_date`.
- Approve: action `approve-draft` with explicit `id` and optional `note`.
- Reject: action `reject-draft` with explicit `id` and optional `note`.

Approval creates a formal event and queues market/return refresh. It requires the user's explicit draft ID and confirmation. For edits to symbol, direction, thesis, evidence, or conditions, direct the user to the UI; do not approve first and repair later.

## Events

- List: action `list-events`.
- Change candidate/excluded state only after explicit confirmation: action `event-action` with `event_id` and `event_action`.
- Exclusion also requires `note`.

Use the UI for event amendments that alter symbol, direction, time, or thesis because those changes may recalculate returns.

## Status Language

- `latest_fetch_*` describes source collection.
- `latest_ai_at` and `pending_ai` describe optional AI processing.
- `delivery_*` describes the morning report; a late or missing delivery is separate from collection health.
- `market_status=closed` can be normal when the latest expected trade date is current.
- `market_status=provider_pending` plus a high `lagging_symbol_count` means the market source is behind. This affects baseline pricing and returns, not post collection.
- A partial fetch is not a completed fetch.
- Codex quota failure is an AI degradation, not a console or collection failure.

### FreeStockDB update rules

FreeStockDB is an optional local market-data source. A healthy HTTP service does not prove that its dataset is current, and a GUI message such as "sync completed" does not prove that the update passed validation.

- When the user asks to update market data, call the documented `kol_operator` market action. Do not launch the vendor updater GUI, run `stockdb.exe` or `数据更新.exe` directly, or edit the live data directory.
- Treat an update as successful only when the operator reports all of: service reachable, expected latest completed trading date, catalog coverage, cross-section coverage, and sample price validation. Otherwise report `stale`, `degraded`, or `failed`, never `success` based on a process exit or window text.
- Check service health and data freshness separately. Before the market session, the latest completed trading date may legitimately be the previous session; after the session it must be compared with the expected trading calendar.
- The canonical storage layout is `<AI_HUB_HOME>\stockdb\data` as a compatibility junction to `<MARKET_DATA_HOME>\free-stockdb\live`. A reversed junction, an unexpected physical copy, a missing `live` generation, or an unverified swap is a maintenance error to hand to Codex.
- The normal update uses an isolated staging generation, validation, and atomic rotation. Do not copy the whole dataset for every run, do not overwrite a verified live generation in place, and do not delete staging or rollback data after a failed update.
- If an update fails, preserve the verified live dataset and the staging/swap evidence for Codex. Do not retry repeatedly, repair the database, change the schedule, or claim that returns are current.
- Keep the FreeStockDB update and the later market/returns jobs from overlapping. If a task is already running, report `already_running`; do not start a duplicate process.
- FreeStockDB is a fallback and minute/cross-section supplement. Its failure must not block BaoStock-based daily returns, but it must remain visible as a data warning.

When the user says "同步已完成" or "更新好了", verify the structured operator/doctor state before confirming. If the state is stale, partial, or failed, report the exact state and delegate any repair or source inspection to Codex.

### Browser pop-ups during collection

`fetch_running=true` means the collector is actively driving Edge/Chrome automation instances to render and scrape X/Zhihu pages. Frequent browser windows while `fetch_running=true` are normal collection activity; do not kill browsers or the console process.

Confirm progress through the operator status: `fetch_running=true` must be accompanied by advancing fetch/AI timestamps, growing preview posts, or a decreasing pending queue. A running status with zero progress is a stuck-fetch case and must be delegated to Codex.

### Board status

For `kol_operator({"action":"board-status"})`:

- `status=partial_coverage` or coverage below 1 means some boards lack RPS data.
- Compare `latest_trade_dates` separately for industry and concept boards; a lagging concept source is not evidence that the whole console is broken.
- `pending_backfill=0` means no queued backfill, not that every historical date is complete; use the reported coverage and latest run state.

For any source inspection or modification, hand off the user request and current status output through `delegate-to-codex`. Do not attempt an improvised repair before, during, or after delegation.

Never invent a fallback command or claim a file is missing based on a failed tool call. Report the exact `kol_operator` error and stop or delegate maintenance to Codex.

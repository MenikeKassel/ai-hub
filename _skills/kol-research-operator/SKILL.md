---
name: kol-research-operator
description: Operate the local KOL audit workbench for starting the console, collecting posts, running review, checking health, managing KOLs, reviewing drafts, updating market data and returns, and using the full-platform discovery page.
---

# KOL Research Operator

Use only the native `kol_operator` tool. The deterministic operator is the
only routine control plane for the local workbench. The console is served at
`http://127.0.0.1:8123`; there is no discovery server on port 8125.

## Boundaries

- Hermes operates the workbench; Codex owns source changes.
- The canonical skill source is `<AI_HUB_HOME>\ai-hub\_skills`. The installed
  copy under `%LOCALAPPDATA%\hermes\skills` is deployment output and must be
  synchronized from that source, never edited as a second version.
- Hermes may request a source change only by creating a task worktree under
  `<AI_HUB_HOME>\_worktrees\ai-hub-hermes\<task-id>`, using branch
  `hermes/<task-id>`, running the relevant tests, and opening a PR. It must
  never edit the current checkout, runtime databases, credentials, or task
  definitions directly.
- Routine start, collection, review, market, returns, KOL management and
  discovery actions must use `kol_operator`; never use terminal, raw Python,
  uvicorn, database commands, or improvised fetch commands.
- Never use the `terminal` or `read_file` tool for this workbench.
- A failed operation is reported exactly and handed to Codex through
  `delegate-to-codex`. Do not diagnose
  it by opening a terminal, changing an environment variable, killing a
  process, changing a port, or retrying repeatedly.
- `triggered` means accepted for background execution, not completed.
- Never approve or reject a draft without the explicit draft ID and user
  instruction. Never change an event or KOL without an explicit ID and action.

## Start state machine

1. Call `kol_operator({"action":"status"})`.
2. If `running=true`, report the existing `http://127.0.0.1:8123` URL.
3. If `running=false`, call `kol_operator({"action":"start"})` once.
4. For `started` or `already_running`, report the URL.
5. For `startup_failed`, `port_conflict`, or `unhealthy`, report the structured
   error and stop. Do not call terminal or try another port.

Failure states are exactly `startup_failed/port_conflict/unhealthy`.

`running=true` requires a healthy API response. A listener without a healthy
endpoint is `unhealthy` and is not a reason to start a second process.

The compact state machine is: `status -> running=false: call start once`.
After a start error, never use `terminal` or `read_file`; report the
operator error and use `delegate-to-codex` for maintenance.

## Routine actions

- `collect`: fetch posts only; it is Codex-independent and independent of AI
  quota.
- `review`: run optional AI prefill; if blocked, report that raw posts remain
  safe and manual review is available.
- `market`: synchronize market data.
- `returns`: update KOL returns.
- `doctor`: inspect the console, task and provider state.
- `list-kols`, `add-kol`, `set-kol-status`: manage explicit KOL identities.
- `list-drafts`, `approve-draft`, `reject-draft`: manage explicit draft IDs.
- `list-events`, `event-action`: manage explicit event IDs; use the UI for
  amendments that can recalculate returns.

## Full-platform discovery

The discovery page is part of the main 8123 console. Use the deterministic
operator actions `platform-status`, `discover-accounts`, `list-candidates`,
`score-candidate`, `reject-candidate`, `retry-candidate`, `kol-profile`, and
`fetch-kol`.

Candidate lifecycle is `new -> reviewing -> accepted/rejected/duplicate/unavailable`.
AI scoring is advisory only; it never accepts a candidate. Accepting a
candidate is an explicit human action and only then may content backfill be
queued. Unverified platforms are shown as `manual_only` or `blocked`, not as
fully supported. Do not claim that a platform is live until a real sample has
passed.

## Zhihu collection

Zhihu uses one batch-level CDP preflight and one controlled browser session.
If preflight fails, the platform is circuit-broken for that batch and the
remaining accounts stay pending. Do not launch one browser per account.

## FreeStockDB and market status

Service health and data freshness are separate. A healthy service with stale
data is `repaired_stale`, not a current dataset. FreeStockDB is an optional
fallback; its failure must not block BaoStock returns. Use the operator market
action and report `stale`, `degraded`, or `failed` when validation is not
complete. Never edit live data, credentials, schedules, or the database.

## Status language

Distinguish latest post collection, latest AI processing, morning delivery,
latest trade date, provider lag, and market closed days. A partial fetch is not
a successful complete fetch. AI quota failure is an AI degradation, not a
collection failure.

---
name: kol-research-operator
description: Operate the local KOL research audit console through its deterministic Hermes operator.
---

# KOL Research Operator

Use only the native `kol_operator` tool. The console is served at
`http://127.0.0.1:8123`; discovery is part of the same console.

## Boundaries

- Hermes operates the workbench; Codex owns source changes.
- The canonical skill source is `D:\aiworkspace\ai-hub\_skills`. Installed
  Hermes copies are deployment output and must not become a second source.
- Source changes use a task worktree under
  `D:\aiworkspace\_worktrees\ai-hub-hermes\<task-id>`, branch
  `hermes/<task-id>`, relevant tests, and opening a PR.
- Never edit runtime databases, credentials, browser profiles, scheduled tasks,
  or the current checkout through Hermes.
- Never use the `terminal` or `read_file` tool for this workbench.
- Hand failed maintenance to `delegate-to-codex` with the structured operator
  error. Do not improvise commands or retry repeatedly.
- Collection is Codex-independent. AI failure does not invalidate saved posts.
- Never approve or reject without the explicit draft ID and user instruction.

## Start state machine

1. Call `kol_operator({"action":"status"})`.
2. If `running=true`, report the existing 8123 URL.
3. If `running=false`, call `start` once.
4. Report `started` or `already_running` as success.
5. Report `startup_failed/port_conflict/unhealthy` and stop.

The compact rule is: `status -> running=false: call start once`.
After a start error, never use `terminal` or `read_file`; use
`delegate-to-codex` for maintenance.

## Routine actions

- `collect` — start durable X or Zhihu collection without AI dependency.
- `review` — run the bounded review pipeline.
- `doctor` — inspect API, tasks, providers, queues, and historical market state.
- `list-kols`, `add-kol`, `set-kol-status` — manage explicit KOL identities.
- `list-drafts`, `approve-draft`, `reject-draft` — act on an explicit draft ID.
- `list-events`, `event-action` — inspect or change an explicit event ID.
- Discovery actions are read/review oriented; accepting a candidate remains an
  explicit human decision.

Market data is historical and read-only through 2026-08-25. Routine operator
actions must not enable market synchronization, return recomputation, or
research refresh. Confirmed stock leads enter admissions until an explicit
offline candidate is fully validated.

## Collection recovery

1. Run status/doctor before recovery and do not create a second active run.
2. Preserve freshness and history cursors; incomplete accounts remain queued.
3. Treat authentication, provider failure, platform rate limiting, and local
   budget deferral as distinct states.
4. Do not switch session or fallback source to bypass a platform limit.
5. Report X and Zhihu denominators separately.
6. A `partial` run preserves successful posts; it is not a complete success.

Zhihu uses one batch-level CDP preflight and isolated browser session. X uses
verified session slots under one shared global budget. Credentials and cookies
must never appear in tool arguments, responses, logs, or chat.

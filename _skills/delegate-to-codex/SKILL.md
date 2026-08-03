---
name: delegate-to-codex
description: Delegate complex Hermes requests to the local Codex CLI for implementation and verification. Use for code changes, debugging and repair, multi-file or batch operations, Git work, Obsidian restructuring, system integration, and other multi-step tasks that require tools or sustained execution. Do not use for ordinary conversation, simple lookups, or the existing capture commands such as /clip, /idea, /log, /kol, /event, /concept, and /holding.
---

# Delegate To Codex

Use Codex as Hermes's execution backend for complex work. Hermes remains the
conversation and capture layer; Codex owns implementation, testing, and a clear
completion report.

Select this skill before `claude-code` or Hermes's own execution tools. Use
`claude-code` only if the user explicitly requests Claude, or if Codex fails and
the user agrees to that fallback.

## Route The Request

Delegate when any of these are true:

- the request changes code or several files;
- diagnosis requires reading logs, source, and runtime state;
- the request includes implementation plus tests or verification;
- the work involves Git, Obsidian restructuring, automation, or system integration;
- the task has three or more dependent execution steps.

Handle directly when the request is conversation, a short explanation, a simple
status check, or one of the established capture commands.

Before delegation, ask for explicit confirmation if the task would delete or
mass-move data, publish or push externally, use credentials or login flows, send
messages, place trades, or make another irreversible/high-impact change.

## Execute

Use only the native `codex_delegate` tool. Never invoke Codex through `terminal`,
`execute_code`, `delegate_task`, raw PowerShell, or raw Python.

- Implementation: `codex_delegate({"task":"<full request and needed context>","mode":"write"})`.
- Inspection only: use the same tool with `mode` set to `read-only`.
- The default workspace is `<AI_HUB_HOME>\ai-hub`; set `workspace` only when the
  request clearly belongs to another project under `<AI_HUB_HOME>`.

Preserve the user's full request and constraints. The native tool encodes the task
before starting the local Codex CLI, so Chinese text and shell characters are not
reinterpreted by a command shell.

The command returns one JSON object. If `ok` is true, relay `answer` and briefly
name any verification Codex reports. If `ok` is false, report `error` and do not
claim completion. Do not silently finish the task yourself after a failed Codex
run; either retry once with corrected context or explain the blocker.

Before starting the command, tell the user that the complex task has been handed
to Codex and may take a few minutes.

## Keep Boundaries

- Keep `/clip`, `/idea`, `/readlater`, `/auto`, `/log`, `/kol`, `/event`,
  `/concept`, and `/holding` on the existing Hermes capture path.
- Do not ask Codex to make buy/sell decisions or place trades.
- Do not include API keys, tokens, cookies, or passwords in the delegated prompt.
- Never inspect or modify source yourself before or after delegation.
- Never fall back to another agent when Codex is unavailable or out of quota.
- Tell Codex to preserve unrelated user changes in dirty worktrees.
- Return Codex's result to the user; Hermes must not invent missing test results.

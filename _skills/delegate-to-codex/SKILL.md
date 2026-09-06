---
name: delegate-to-codex
description: Delegate complex Hermes implementation and diagnosis to the local Codex tool. Use for code changes, debugging and repair, multi-file or batch work, Git work, system integration, and other multi-step tasks that require tools or sustained execution. Do not use for ordinary conversation, simple lookups, or the existing capture commands such as /clip, /idea, /readlater, /auto, /log, /kol, /event, /concept, and /holding.
version: 2.0.0
source: This file is the canonical source (`_skills/delegate-to-codex`). Installed Hermes copies are deployment output produced by `scripts/install-hermes-codex-delegate.ps1`; never edit the installed copy as a second version, and never copy installed content back into this file.
---

# Delegate To Codex

Use only the native `codex_delegate` tool for code changes, debugging, Git,
multi-file work, automation, and system integration. Hermes remains the
conversation layer; Codex owns implementation, testing, and reporting.
Execution method obeys the central routing block in `SOUL.md`
(`<!-- ai-hub:codex-routing -->`); this skill defines the Codex entry, and
generic skills must not define a second execution entry.

Default workspace: `D:\aiworkspace\ai-hub`.

- Use `mode=write` for authorized implementation.
- Use `mode=read-only` for inspection and diagnosis.
- Preserve the user's request and unrelated working-tree changes.
- Never include API keys, tokens, cookies, passwords, or private runtime rows
  in the delegated prompt.
- Never inspect or modify source yourself before or after delegation; return
  Codex's verified result and do not claim work that Codex did not complete.
- If `ok=false`, report `error` and retry once with corrected context, or
  explain the blocker; never silently finish the task yourself after a failed
  Codex run.
- Do not delegate trading decisions or broker actions.
- If Codex is unavailable or out of quota, report the blocker and leave source
  unchanged; do not fall back to another agent on your own.

## Claude fallback

Use `claude-code` only when the user explicitly requests Claude, or when Codex
fails and the user agrees to that fallback. Unavailability or out-of-quota is
a stop condition, not a fallback trigger.

## Approval timing

Before delegating a task that would delete or mass-move data, publish or push
externally, use credentials or login flows, send messages, place trades, or
make another irreversible/high-impact change, obtain the user's explicit
confirmation for that final action.

Already authorized preparation — reading, inspecting, implementing, testing,
drafting the diff — may proceed before that confirmation. The irreversible
action itself is executed only after explicit approval at execution time.
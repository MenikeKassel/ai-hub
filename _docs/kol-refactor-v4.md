# KOL console v4 refactor

The daily private console remains at `http://127.0.0.1:8123`.
This refactor preserves the existing runtime data, keeps automatic return updates enabled,
and leaves research analysis on manual execution.

## Delivery checkpoints

1. Correct statistics, isolated tests, compact read projections.
2. Separate API routes, post processing modules and CLI command groups.
3. Five workspaces, URL state, lazy detail loading and operational status.
4. Offline regression, runtime backup, idle service cutover and verification.

The pre-change source, frontend distribution and existing working patch are backed up outside Git
under `D:\aiworkspace\_kol-repair-backups\refactor-20260908-095119`.
The initial baseline is 61 selected backend tests, 12 frontend tests and TypeScript validation.

Existing `/api/*` and CLI commands remain compatibility interfaces. Runtime data does not enter
Notion, Obsidian, the public workbench or Git.

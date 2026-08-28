# Documentation index

Use this index instead of assuming every historical handoff still describes the
current machine.

## Current operating documents

- `glm-review.md` — entrypoint and review contract for GLM or another reviewer.
- `architecture-current.md` — current components, data flow, and invariants.
- `operations-current.md` — startup, health, tasks, tests, and recovery.
- `../README.md` — repository purpose and quick start.
- `../CHANGELOG.md` — versioned behavior changes.
- `../SECURITY.md` — security and disclosure boundary.
- `../CONTRIBUTING.md` — Git, test, and review workflow.

## Domain references

- `kol-market-data-v3.md`
- `kol-performance-v3.md`
- `kol-research-console-v2.md`
- `kol-discovery-v3.md`
- `freestockdb-market-provider-v1.md`
- `platform-reader-research.md`

These references contain useful design history. Where they conflict with the
current operating documents, the current documents and executable tests win.

## Historical handoffs

Files named `*-handoff-*`, the original A-share implementation plans, and docs
containing E/F-drive paths are retained as audit history. They are not current
startup instructions and should not be applied mechanically.

## Documentation rules

- Put current state in the three current documents above.
- Put behavior changes in `CHANGELOG.md`.
- Mark dated investigations and handoffs with an explicit date.
- Do not paste runtime payloads, credentials, cookies, database rows, private
  account IDs, or full local logs into documentation.

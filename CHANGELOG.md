# Changelog

All notable source and operational changes are recorded here. Runtime data,
credentials, databases, media, and generated reports are deliberately excluded
from Git.

## 3.0.0 - 2026-08-28

### Added

- A durable historical-market admission queue for confirmed KOL stock leads.
- A shared historical-market write policy used by the API and CLI.
- Persistent X session slots, request budgets, low-frequency rotation, and a
  cookie-free single-post backup gate.
- Persistent OCR/model daily budgets and resumable batch classification.
- Lightweight `/api/system/health` plus opt-in deep diagnostics.
- Current architecture, operations, documentation index, and GLM review guide.

### Changed

- The KOL console is consolidated on `127.0.0.1:8123`; legacy port 8765 stays
  stopped.
- Market data is historical and read-only at `2026-08-25`. Formal coverage is
  defined by a verified manifest; incomplete symbols remain in admissions.
- X runs distinguish local budget deferral, provider failure, authentication,
  and platform rate limiting. Incomplete queues resume later.
- Zhihu uses one isolated Chrome CDP session on port 9223 with a keeper tab.
- Scheduled OCR processes at most 150 posts per day; model classification
  processes at most 250 posts per day.
- Provider timestamp conflicts are persisted on authoritative full-content
  posts so approval is blocked until the source conflict is reconciled.

### Security

- Runtime databases, DuckDB/Parquet data, credentials, cookies, logs, PID files,
  browser profiles, external repositories, and private capture configuration
  are ignored by Git.
- Hermes capture now ships a sanitized `config.example.yaml`; the real
  `config.yaml` remains local-only.

### Known review items

- `kol_posts.py`, `trading_cli.py`, and `kol_api.py` are still large modules.
  Policy, admissions, and model-budget domains were extracted first; further
  splitting is intentionally incremental.
- Four X handles can return a generic upstream `TwitterAPIError`; the adapter
  currently preserves retry state but does not expose a sanitized upstream
  status code.
- Existing legacy documentation contains historical E/F-drive references. The
  documentation index identifies those files as historical rather than current
  operating instructions.

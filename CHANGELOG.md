# Changelog

All notable source and operational changes are recorded here. Runtime data,
credentials, databases, media, and generated reports are deliberately excluded
from Git.

## 3.2.0 - 2026-08-30

### Added

- Explicit unlimited X primary/public-backup mode with request accounting,
  batch leases, authentication handling, and real-429 cooling retained.
- Idempotent `kol-backup-upgrade` and `kol-draft-materialize` maintenance
  commands for selected backup evidence and saved GLM classifications.
- Backup-upgrade and multi-source X regression fixtures.

### Changed

- Restored event/audit history and missing media only when the backup path is
  absent from the current runtime; current content and conflicting files remain
  authoritative.
- System API and UI report unlimited mode explicitly instead of displaying fake
  remaining quotas or hard-coded per-session limits.
- OCR backlog throughput is explicitly unlimited locally; durable 50-item
  batching and failure accounting remain active.
- The scheduled classifier now passes `--ocr-limit 0` to make the unlimited
  OCR policy explicit; bounded-mode callers remain supported.

### Security

- Unlimited mode intentionally removes local volume and interval safeguards.
  Authentication failures and upstream 429 responses still stop or cool down
  the affected source; this mode does not guarantee account safety.

## 3.1.0 - 2026-08-29

### Added

- Atomic `market-daily-publish` maintenance command with BaoStock primary and
  local/public fallbacks, including Beijing-exchange symbols.
- Live market-only mode flags and a daily 17:50/19:30 validation-publication
  schedule; returns and research writes remain disabled.
- Resumable OCR queue recovery with a persistent 150-item daily cap and 50-item
  batches, plus safer scheduled stdout/stderr capture.

### Changed

- Market publication now advances only after an isolated candidate reaches the
  latest completed trading date and passes integrity checks; failures leave the
  previous directory and recovery mode untouched.
- Slot 2 X credentials were verified as a distinct account; shared global and
  per-slot budgets remain enforced.

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

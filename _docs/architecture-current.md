# Current architecture

The market boundary is now a daily, atomic publication boundary. BaoStock is
the primary source; FreeStockDB remains the local supplementary reader and
Tencent/AKShare are fallbacks for symbols BaoStock cannot serve. Publication
sets `mode=live`, `write_enabled=true`, `market_update_enabled=true`, while
`returns_update_enabled=true` and `research_update_enabled=false`. Candidate directories and the
previous published directory make each run recoverable.

Status date: 2026-09-26. Source version: 3.1.0.

## Purpose and boundary

The project collects public KOL evidence, classifies candidate A-share research
content, supports human approval, and displays the published daily market context. It
does not connect to a broker, place orders, issue trading signals, or copy KOL
positions.

## Runtime topology

```text
X session pool ─┐
                ├─> KOL collection ─> posts.db ─> rules/OCR/model ─> review
Zhihu CDP 9223 ─┘                                      │
                                                      └─> stock leads
                                                            │
                                             confirmed admissions
                                                            │
BaoStock / FreeStockDB 7899 / Tencent / AKShare ─> candidate validation ─> market manifest

FastAPI + built React UI: 127.0.0.1:8123
Nitter shadow service:    127.0.0.1:9377
```

## Operational contract

The failure-to-guardrail map and remaining boundaries are in
[`kol-architecture-guardrails.md`](kol-architecture-guardrails.md).

- `scripts/kol-task-contract.json` is the desired schedule for KOL collection
  and morning orchestration. `install-kol-recovery-tasks.ps1` is its sole task
  owner. Market and research tasks belong to `install-research-data-tasks.ps1`.
  The legacy post-fetch installer delegates to the owner.
- `scripts/kol-task-doctor.ps1` compares the contract with installed Windows
  tasks and warns on live X policy deviation. Source changes alone do not alter
  installed triggers, actions, or the operator-approved X budget override.
- Collection coverage counts distinct active accounts and their latest
  successful fetch. Fetch queue rows are per batch and may repeat one account;
  archived rows remain in SQLite for audit.
- A market lead replay table in `posts.db` persists confirmed symbols before
  touching DuckDB. A lock timeout or process interruption leaves those symbols
  for the next successful reconciliation.
- Schema changes use numbered, additive migrations. Existing databases and
  newly created databases must both pass migration tests.

## Source layout

- `_automation/trading_research/` — API, CLI, stores, classifiers, market and
  event research.
- `_automation/trading_research/ui/` — React console.
- `_automation/hermes-capture/` — Hermes capture and isolated Zhihu browser
  integration.
- `scripts/` — startup, scheduled-task, doctor, and publication entrypoints.
- `_infra/` — local infrastructure templates.
- `_docs/` — current docs plus dated design history.

Ignored local state:

- `_runtime/` — SQLite, DuckDB, Parquet, media, logs, manifests, queues.
- `_external/` — third-party repositories and managed tools.
- Windows Credential Manager — X, Nitter, and model credential values.

## Critical invariants

### Posts and review

- `post_id` is the deduplication key.
- Existing non-empty body, media, raw response, and content hash fields are not
  overwritten by recovery data.
- Empty public-dataset records may be hydrated, but inaccessible content is not
  fabricated.
- AI creates evidence drafts; a human approves formal recommendation events.
- The review workbench exposes only the current 09:00-to-09:00 window and the
  next morning preview. Historical drafts remain readable through audit and
  event history, but cannot re-enter an actionable queue or be retried.
- Morning orchestration, recommendation reprocessing, and repair may create
  drafts only for those two active windows. Older candidates are marked
  `historical_archive_only` instead of becoming a persistent backlog.

### Daily market

- `recovery-mode.json` defines `mode=live`, the latest completed close, and
  `publication_mode=atomic_daily`.
- The guarded daily publisher may write market data. The weekday return tracker
  runs after publication; event research stays manual.
- Confirmed leads enter `market_symbol_admissions`; they do not directly change
  formal market coverage.
- A symbol is published only after raw and qfq daily series both pass the cutoff
  validation. Publication uses a candidate and rollback directory.
- BaoStock supplies the primary trading calendar. If its login is unavailable,
  Tencent's `000001` daily series supplies the open dates; the persisted calendar
  is the last audited fallback. One failed BaoStock login opens a circuit breaker
  for that process so every symbol does not repeat the same slow failure.
- Tencent volume is normalized from lots to shares. Its `qfqday` payload is read
  separately from raw `day`. For Beijing 920 symbols that expose only raw data,
  the publisher may use an identity-qfq fallback only after at least five recent
  shared sessions prove raw and qfq OHLC are identical; the derived provider is
  recorded as `tencent_identity_qfq` in the run audit.
- FreeStockDB is loopback-only supplementary storage. Its updater stages and
  verifies both vendor databases before an atomic swap. A stale vendor candidate
  never replaces the last accepted generation and never weakens publication
  freshness requirements.

### X collection

- Three credential references are supported; only verified, enabled, distinct
  identities rotate.
- One batch leases one session. A failed batch is not continued on another
  session to bypass platform limits.
- The global rolling limit is 180 estimated requests per 24 hours; one identity
  is limited to 90. Increasing session count does not raise the global limit.
- Authentication, platform rate limiting, provider errors, and local budget
  deferral are separate states. Queues are resumable.
- The public FxTwitter adapter is single-post only and cannot replace timeline
  collection.

### Zhihu collection

- One isolated Chrome profile exposes CDP on port 9223.
- A keeper `about:blank` tab prevents the browser from exiting between account
  captures; request tabs are temporary.
- A batch validates the browser and login once, then persists account progress.

## Public interfaces that require compatibility

- `/api/kols`, `/api/posts`, `/api/events`, `/api/stock-leads`
- `/api/market/health`, `/api/market/admissions`
- `/api/system/health`, `/api/system/diagnostics`
- `/api/system/x-sessions`, `/api/system/public-backup`
- CLI commands documented by `python trading_cli.py --help`

Database migrations and API changes must be additive unless a versioned
migration and rollback plan is included.

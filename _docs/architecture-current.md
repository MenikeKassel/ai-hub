# Current architecture

The market boundary is now a daily, atomic publication boundary. BaoStock is
the primary source; FreeStockDB remains the local supplementary reader and
Tencent/AKShare are fallbacks for symbols BaoStock cannot serve. Publication
sets `mode=live`, `write_enabled=true`, `market_update_enabled=true`, while
returns and research update flags stay disabled. Candidate directories and the
previous published directory make each run recoverable.

Status date: 2026-08-29. Source version: 3.1.0.

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
CSV / FreeStockDB 7899 / BaoStock ─> candidate validation ─> market manifest

FastAPI + built React UI: 127.0.0.1:8123
Nitter shadow service:    127.0.0.1:9377
```

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

### Daily market

- `recovery-mode.json` defines `mode=live`, the latest completed close, and
  `publication_mode=atomic_daily`.
- The guarded daily publisher may write market data; returns and research
  refresh APIs remain HTTP 409.
- Confirmed leads enter `market_symbol_admissions`; they do not directly change
  formal market coverage.
- A symbol is published only after raw and qfq daily series both pass the cutoff
  validation. Publication uses a candidate and rollback directory.

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

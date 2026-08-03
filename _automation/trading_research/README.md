# A-share Trading Research and Audit v1

This module is a read-only research helper for the Obsidian trading system.
It does not connect to brokers, place orders, or produce position advice.

## Public core adapter

The reusable, anonymized engine now lives in
`MenikeKassel/kol-audit-workbench`. This private directory keeps personal
integrations and historical runtime data. New shared features should be added
to the public package and consumed through `public_core_adapter.py` instead of
copying modules back into this repository.

```powershell
pip install -r requirements-public-core.txt
python public_core_adapter.py doctor
python public_core_adapter.py demo
python public_core_adapter.py serve
```

The adapter uses `_runtime\trading\public-core` by default, so it cannot mutate
the existing private KOL database or event ledger during the migration period.

## Role

- Seed and maintain a small A-share watchlist.
- Fetch daily price data with AKShare when available.
- Optionally cross-check prices with Baostock when installed.
- Generate Obsidian-friendly audit reports.
- Keep runtime data under `<AI_HUB_HOME>\ai-hub\_runtime\trading`.
- Treat ETFs as a separate instrument type. The first real ETF audit target is
  `159139`, opened on 2026-07-07 at 1.460. Do not send ETF codes through a
  stock-only data endpoint.

## Commands

```powershell
python trading_cli.py init
python trading_cli.py fetch-prices --start 20240101 --end 20260703
python trading_cli.py verify-baostock --symbols 600900,600519 --start 20240101 --end 20260703
python trading_cli.py generate-report
python trading_cli.py doctor
python trading_cli.py kol-init
python trading_cli.py kol-doctor
python trading_cli.py kol-register --source-note <path> --kol <name> --symbol <code> --direction long --posted-at <ISO-time>
python trading_cli.py kol-update --as-of 2026-07-13 --dry-run
python trading_cli.py kol-update --as-of 2026-07-13 --notify
python trading_cli.py kol-report
python trading_cli.py kol-context-doctor
python trading_cli.py kol-context-backfill --all
python trading_cli.py kol-context-backfill --event-id <event-id> --force
python trading_cli.py kol-method-research-doctor
python trading_cli.py kol-method-research-backfill --all --with-minute --with-ai
python trading_cli.py kol-method-research-run --pending
python trading_cli.py kol-post-doctor
python trading_cli.py kol-post-fetch --backfill 100
python trading_cli.py kol-post-fetch --as-of 2026-07-13 --notify
python trading_cli.py kol-post-classify --pending
python trading_cli.py kol-review-agent-doctor
python trading_cli.py kol-review-agent-run --mode shadow --max-runtime 25
python trading_cli.py kol-review-agent-run --post-id <post-id> --dry-run
python trading_cli.py kol-review-agent-report
python trading_cli.py kol-morning-doctor
python trading_cli.py kol-morning-run --as-of 2026-07-17
python trading_cli.py kol-morning-run --as-of 2026-07-17 --skip-fetch
python trading_cli.py kol-morning-migrate
python trading_cli.py kol-ui-doctor
python trading_cli.py kol-leads-extract --pending
python trading_cli.py market-init
python trading_cli.py market-doctor
python trading_cli.py market-freestockdb-doctor
python trading_cli.py market-backfill --symbols "600900,159139,000300" --start 2024-01-01
python trading_cli.py market-minute-fetch --symbol 600900 --start 2026-07-01 --end 2026-07-03 --frequency 1m
python trading_cli.py market-sync --as-of 2026-07-14
python trading_cli.py market-audit --symbols "600900,159139,000300" --cross-check
python trading_cli.py market-weekly --as-of 2026-07-14
.\fetch_external_tools.ps1
```

Use the existing `<AI_HUB_HOME>\lianghua\.venv` if the default Python does
not have `akshare` installed:

```powershell
& '<AI_HUB_HOME>\lianghua\.venv\Scripts\python.exe' trading_cli.py doctor
```

## KOL return tracker

The tracker stores mutable audit data under `_runtime\trading\kol`:

- `events.csv`: candidate, active, completed, and excluded events.
- `daily_marks.csv`: one daily price and return mark per event.
- `checkpoints.csv`: immutable 5/20/60/120 trading-day results.
- `runs.jsonl`: update, notification, error, and manual-change ledger.
- `backups\`: timestamped event-register backups.
- `logs\`: scheduled-task output with rotation.

BaoStock supplies the normal raw and forward-adjusted A-share series. AKShare
is the fallback and checkpoint verifier. A checkpoint is frozen only when the
stock and CSI 300 baseline/target prices agree within 0.5% across both sources.
Raw, unadjusted prices remain the official KOL performance measure; adjusted
returns are diagnostic only.

Formal events also receive a versioned technical-context snapshot using the
last complete forward-adjusted bar available when the post was published. The
snapshot contains RSI14, MACD, ATR%, five-day volume ratio, 20-session return,
and distance from the 60-session closing high. It is audit context only: it does
not score an event, approve a recommendation, or change KOL performance. Daily
marks and frozen checkpoints retain both MFE and MAE.

Each formal event also receives an append-only, point-in-time multi-method
research snapshot. Five lenses remain independent: short-term leader evidence,
Dow/wave/Gann structure, PA price behavior, ICT time/liquidity proxies, and
Wyckoff/order-flow context. There is no combined technical score. The objective
layer uses forward-adjusted daily bars, event-time-truncated minute bars, board
RPS, and immutable FreeStockDB market cross sections. Forward-adjusted OHLC is
rebased so the cutoff-day close equals that day's real unadjusted close; this
keeps pre-event continuity without letting a later dividend silently rescale
the displayed event-time price. The short-term leader
lens reports four transparent candidate types: limit-up leader, trend leader,
liquidity core, and board leader.

The full-market cross section is a bounded event-vintage exception, not a daily
factor scanner. It only caches the 21 completed sessions needed to reconstruct
formal recommendation dates, is shared across events, and never ranks today's
market or emits a trading candidate list. Raw and normalized snapshots are
content-addressed; a replacement with less than 90% of the last accepted
coverage is quarantined instead of replacing the accepted read source.

VWAP uses actual minute OHLCV. VP and TPO are explicitly labeled minute-bar
approximations. CVD remains unavailable without aggressor-side trade data, and
an ordinary A-share stock has no applicable stock-specific option wall. Wave
counts, Gann structures, Wyckoff phases, and ICT patterns are optional AI
hypotheses generated by the configured DeepSeek V4 Flash API. Codex is not an
automatic fallback, so these research jobs cannot consume Codex quota. Every
hypothesis must cite a real field in the objective snapshot; failed validation
is retained as an audit record while the objective evidence remains usable.

The daily board job triggers `KOL_Event_Method_Research` only after its accepted
snapshot and RPS work completes. A 02:00 trigger is retained as a fallback when
the board source fails or the machine resumes late. The research task processes
a bounded persistent queue. Re-running identical inputs is idempotent, while an
event symbol, direction, thesis, name, or timestamp amendment invalidates the
matching AI interpretation and preserves every prior version.

Run market and context CLI commands serially. DuckDB protects the warehouse
from cross-process writers, while the scheduled pipeline already preserves the
required market-sync, context-backfill, and return-update order.

`kol-register` is an administrator interface. Hermes `/event` must continue to
create source candidates only. One activated event must have one KOL, platform,
source URL, exact timezone-aware post time, one six-digit A-share symbol,
direction, and thesis. Multi-stock posts must be split after human review.

The generated local report is
`_runtime\trading\kol\reports\KOL推荐收益看板.md`. The tracker does not write
Obsidian or rewrite the historical manual `KOL推荐事件表.md`.

Install the weekday 20:00 task and dedicated environment from PowerShell:

```powershell
& '<AI_HUB_HOME>\ai-hub\scripts\install-kol-tracker.ps1'
```

The task is named `KOL_Return_Tracker_Daily`, starts when a missed run becomes
available, and uses a mutex to prevent overlap. Normal daily changes update
the local report silently. Hermes sends Feishu messages only for activation,
checkpoints, completion, source conflicts, or task failures.

## KOL research console v2

The local FastAPI + React console reads the existing return CSV files and keeps
captured X posts in `_runtime\trading\kol\posts.db`. Raw posts and approved
events do not enter Notion or Obsidian. Human approval binds the event directly
to the local post record and registers an active return event without network
dependencies.

```powershell
& '<AI_HUB_HOME>\ai-hub\scripts\install-kol-post-fetch.ps1'
& '<AI_HUB_HOME>\ai-hub\scripts\start-kol-ui.ps1'
```

Open `http://127.0.0.1:8123`. Configure `auth_token` and `ct0` on the System
page. Values are stored in Windows Credential Manager under
`ai-hub/twitter-cli`; they are never written to repository files or logs.

The X timeline collector now has two independent providers. `twitter-cli`
remains the primary source and receives the local proxy through
`TWITTER_PROXY`. A pinned `x-tweet-fetcher v3.0.0` installation reads the
loopback-only Nitter service at `http://127.0.0.1:9377`. The backup account is
stored separately under `ai-hub/nitter` and is materialized as a private
`sessions.jsonl` file only while starting Nitter.

```powershell
& '<AI_HUB_HOME>\ai-hub\scripts\install-docker-desktop.ps1'
& '<AI_HUB_HOME>\ai-hub\scripts\install-x-tweet-fetcher.ps1'
& '<AI_HUB_HOME>\ai-hub\scripts\install-kol-nitter-task.ps1'
& '<AI_HUB_HOME>\ai-hub\scripts\start-kol-nitter.ps1'
```

Automatic collection starts in `shadow` mode. Both providers are queried, but
only primary-source posts can change the canonical post record. Run
`kol-fallback-mode` to inspect coverage. The `enabled` mode is rejected until
three successful shadow days cover every active KOL and each run has at least
95% matching post IDs.

```powershell
python trading_cli.py kol-post-fetch --provider auto --dry-run
python trading_cli.py kol-nitter-doctor
python trading_cli.py kol-fallback-mode
python trading_cli.py kol-fallback-mode --set enabled
```

Every post keeps its canonical and metrics providers. Per-provider snapshots,
fetch attempts, shadow comparisons, time conflicts, and gap warnings are stored
in SQLite. A lower-priority source can refresh metrics but cannot replace a
higher-priority body. Timestamp conflicts block approval.

Authenticated Zhihu aggregation sources use the existing local Edge CDP profile
through `zhihu_profile_capture.py`. The collector reads profile answers by
answer ID and routes them independently from X, so an X authentication failure
does not block Zhihu. Aggregated answers are always stored as
`secondhand_aggregation`: named bloggers are extracted into
`digest_attributions` and shown on the KOL management page, but neither the
summary nor its attributed authors can directly create a return event. An
original post URL must be recovered and reviewed before any attributed opinion
can enter performance tracking. Manually verified author homepages are stored in
`digest_author_profiles` with `linked_only` status. They provide an original-
source lookup path without silently enabling one crawler per attributed author;
the profile link and the aggregation source remain visible side by side in the
KOL management page.

```powershell
python trading_cli.py kol-post-fetch --platform zhihu --backfill 100 --skip-classify
```

`KOL_Post_Fetch_Daily` runs every day at 19:00, then immediately performs AI
preprocessing into the next morning's preview. One `KOL_Morning_Pipeline` task
has 07:20, 08:20, and 08:45 triggers. Its orchestrator selects the necessary
`initial`, `refresh`, or `final` phase and chains a later phase when the current
run crosses its deadline. A late boot runs one final catch-up rather than three
competing tasks. Runs left in `running` beyond the execution budget are marked
`interrupted`. Each run has its own log file; the summary log is file-locked.
The final phase records account coverage and an explicit `ready`, `degraded`,
`late`, or `missed` 09:00 delivery state. The old
08:00 `KOL_Review_Agent` task is unregistered to avoid competing for the same
post queue. The existing weekday 20:00 return tracker remains separate.

The workbench status strip shows four independent clocks: latest post collection,
latest AI processing, morning delivery, and latest market trading date. A
weekend or exchange holiday is shown as `closed`, not as stale market data.
Human corrections require an error category and are appended to
`draft_revisions`. A missed stock can be added from the post detail with
verbatim evidence. Active and completed events can be amended without silent
overwrites; symbol, direction, or timestamp changes back up and recalculate the
event's returns. The history archive uses server-side search and 50-row pages.

New accounts backfill 100 posts. Routine runs fetch 50 and use finite retry for
rate limits. The UI can queue a durable 200-post history fetch for any account;
it advances in cumulative 100-post depths and preserves completed depth across
interruptions. Failed or gap-detected attempts stay queued. Failed images and
interrupted approval stages are recorded for repair instead of silently
disappearing. The UI can add, edit, pause, and resume accounts; it cannot delete
their history.

The initial enabled handles are `agudianjinshou`, `WwQQ129146`, `sszcw`,
`bafeite1234`, `Mimiwftt`, and `Hoyooyoo`. Serenity is intentionally not
auto-followed until the original account identity is confirmed.

## Morning recommendation workflow v4

The normal operator surface is now `Today Review`:

```text
X post -> rules/OCR -> Codex batch classification -> one draft per stock
       -> human edit/approve/reject -> formal event -> market and returns refresh
```

`recommendation_drafts` is the only user review queue. A multi-stock post has
one independently reviewable row per stock. Approval validates the instrument,
registers an idempotent formal event, starts 120-session tracking, and queues a
market/return refresh. It does not require a confirmed `stock_leads` row.
Every draft also records the author's action, stated horizon, recommendation
strength, and entry conditions. Missing attitude details remain `unspecified`;
the classifier is not allowed to infer a holding period that the author did not
state.

`stock_leads` remains a searchable mention index for recommendations, holdings,
analysis, retrospectives, and secondhand references. It is read-only in the
normal UI and has no pending-task badge. Legacy non-candidate posts are marked
as system-screened with an audit record; source posts are never deleted.

The morning window has priority over historical backlog. Numbered recommendation
lists are parsed deterministically before OCR or Codex. RapidOCR runs locally
through an isolated ONNX runtime, handles at most eight posts per scope, and has
a 90-second batch ceiling. OCR text, line boxes, confidence, and provider are
kept with the classification audit. Unlimited-OCR remains an experimental deep
document parser and is not part of the morning SLA. Codex processes
up to ten posts per batch, retries once, and may only emit structured drafts
with source evidence. Item failures degrade the run but do not prevent other
drafts from being published.

Market runtime data lives under `_runtime\trading\market`: immutable raw
snapshots, normalized Parquet, DuckDB catalog/coverage, manifests, and quality
audits. BaoStock is the normal daily provider and AKShare is the first fallback.
The optional local FreeStockDB service (`FREESTOCKDB_ROOT`, default
`<AI_HUB_HOME>\stockdb`; `FREESTOCKDB_URL`, default `http://127.0.0.1:7899`)
is the third daily freshness provider and the minute-bar supplement. The
runtime manager checks the exact executable path, loopback port, manifest,
catalog size, disk guard, and smoke symbols before accepting the service. Its
configured HTTP mirror is explicitly marked `untrusted_transport`: it can fill
new dates and provide event-window minutes, but it cannot be the sole authority
for a frozen return checkpoint. Install the weekday 17:50 verified update task
with `scripts\install-freestockdb-task.ps1`; first validate it with
`python trading_cli.py market-freestockdb-doctor` and
`python trading_cli.py market-freestockdb-update --dry-run`. A successful update
uses an A/B dataset rotation, manifest/file verification, a one-generation rollback,
and an isolated loopback restart. The first run copies the active dataset into a
private staging tree; later runs reuse the previous verified generation and do not
copy the whole database again. After that bootstrap snapshot, the current generation
stays available while the isolated candidate updates and pauses only for the final
swap. Free-space checks include the bytes still missing from a resumable candidate
plus a 5 GB guard. On Windows, `<AI_HUB_HOME>\stockdb\data` must be the
junction pointing to the real `<MARKET_DATA_HOME>\free-stockdb\live` directory. A reversed
junction is reported as `storage_layout=reversed` and blocks updates. If an existing canonical
daily batch is healthy, FreeStockDB only appends dates after that batch's end and
cannot overwrite it. Minute snapshots are stored separately under
`warehouse\minute_<frequency>` and never enter KOL return calculations.
The bundled Windows release reads `stockdb.conf` and is therefore started from
its own directory without source-build command-line flags; set
`FREESTOCKDB_SERVER_ARGUMENTS=1` only for a compatible source build.
Event-window minutes can be audited with `kol-intraday-backfill --all` and
`kol-intraday-audit`; the resulting context is displayed beside each formal
recommendation without creating a trading signal.
AKShare failures are warnings unless a required cross-source checkpoint conflicts
by more than 0.5%.
The DuckDB master catalog uses exchange-qualified contract keys to preserve
stock/index code collisions while the active registry remains symbol-oriented.

The KOL management page includes a staged leaderboard built only from verified,
executable frozen checkpoints. Fewer than ten 1M samples never receive a rank;
10 verified 1M events are provisional, 20 verified 3M events are reliable, and
10 verified 6M events form the long-term tier.

Install the 19:30 market sync, single 07:20/08:20/08:45 morning orchestrator, 02:30 combined digest,
Sunday weekly refresh, and isolated RapidOCR runtime with
`scripts\install-research-data-tasks.ps1`. To install only OCR, run
`scripts\install-fast-ocr.ps1`.

## Legacy review agent

The review agent remains available for historical shadow-decision inspection
and rollback, but it is no longer the scheduled operator workflow. It reuses
the transparent rule classifier, local RapidOCR, Codex JSON output,
instrument master, and existing event registrar. SQLite keeps each run and
decision under `review_agent_runs`, `review_agent_decisions`, and
`review_agent_settings`.

The default mode is `shadow`: decisions are visible in the review console but
cannot mutate posts, events, market data, or returns. Automatic approval is
limited to one timezone-aware, text-only, long A-share recommendation with
verbatim evidence, no conditions, no source conflict, and at least 0.95 model
and draft confidence. Images, OCR dependencies, multiple stocks, short views,
conditional entries, and ambiguous names remain human work. High-confidence
retrospectives and secondhand posts may be excluded; unrelated or methodology
posts without a stock draft may be ignored.

Candidate scheduling prioritizes text-only posts before image posts. A single
RapidOCR stage is capped at 90 seconds and no more than eight image posts
are attempted in one run, so one difficult image cannot block the text queue.
Classifier prompt v3 keeps Chinese summaries, recommendation reasons, and
conditions in Chinese while preserving verbatim evidence spans.

Market health compares the latest published daily bar with the latest open
session. When BaoStock has not published the just-closed session and the
AKShare fallback is unavailable, the audit page reports one shared provider
lag and the affected symbol count instead of implying that individual stocks
silently stopped tracking. Returns never invent a missing close.

Enabled mode is blocked until shadow mode has run for at least seven days and
produced 100 decisions, including 30 predicted approvals. All predicted
approvals must have human outcomes, approval accuracy must be at least 98%, and
stock/direction corrections must remain at zero.
For the first 30 days, a deterministic 10% sample of otherwise eligible
automatic approvals remains pending for human inspection. Rolling back an
automatic approval preserves the event as excluded, returns the post to
pending, restores any agent-confirmed lead and the instrument's prior research
lifecycle, queues a Feishu alert, records the audit trail, and switches the
agent back to a fresh shadow-validation window. Historical market data remains
intact. Prior validation results cannot be reused immediately after a critical
error.

## Boundaries

- `daily_stock_analysis` is treated as an external report engine.
- `<AI_HUB_HOME>\lianghua\person` is a learning/backtest sandbox.
- KOL candidates and their review state remain in the local SQLite ledger.
- The tracker audits observable recommendation performance; it does not infer
  tradeability or recommend following a KOL.
- AI reports are evidence inputs, not decisions.
- Real positions are recorded as audit facts, not recommendations.
- Personal broker transactions live in the ignored local `portfolio` runtime.
  Import a normalized JSON export with `portfolio-import --file <path>` and
  inspect moving-average positions, realized trading P&L, and dividends with
  `portfolio-report`. Results exclude fees until the broker export supplies
  explicit commission, tax, and transfer-fee fields.

## Current Verification

- Baostock worked for `600900` and `600519` on 2026-07-03.
- AKShare failed on 2026-07-03 because the Eastmoney endpoint connection was reset.
- `daily_stock_analysis` clone failed on 2026-07-03 because GitHub reset the connection.
- ETF daily data is supported by the v3 market layer; `159139` is the first
  live ETF coverage and audit target.
- Re-run `fetch_external_tools.ps1` when GitHub connectivity is stable.

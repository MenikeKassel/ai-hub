# Current operations

Status date: 2026-09-11.

## Start and inspect

```powershell
$repo = "D:\aiworkspace\ai-hub"
& "$repo\scripts\start-kol-ui.ps1" -RepoRoot $repo -Port 8123 -NoBrowser
Invoke-RestMethod http://127.0.0.1:8123/api/system/health
Invoke-RestMethod http://127.0.0.1:8123/api/system/diagnostics
```

Expected local listeners:

- 8123 — KOL API and UI.
- 7899 — FreeStockDB historical reader.
- 9223 — isolated Zhihu Chrome CDP while active.
- 9377 — Nitter shadow service.
- 8765 — stopped.

## Scheduled workflow

- 06:30 — Zhihu collection.
- 07:20 — X morning collection.
- 08:05 — Zhihu refresh.
- 08:45 — review-only morning finalization.
- 09:15 — bounded OCR/model backlog.
- 17:50 — FreeStockDB validation/update (local D: drive).
- 19:00 — X evening collection.
- 19:20 — Zhihu evening collection.
- 19:30 — atomic daily market publication through BaoStock and fallbacks.
- After a successful market publication — audited KOL event return update.
- 23:30 — fallback return update if the publication-triggered run did not occur.

The daily market publisher and KOL return tracker are the scheduled market write tasks. Event
research and performance recomputation remain manual.
After a successful first catch-up the mode is `live` with `as_of` set to the
latest completed trading day; `returns_update_enabled` and
`market_update_enabled` are `true`, while `research_update_enabled` remains `false`.

## Health interpretation

- `success` — all selected accounts completed.
- `partial` — some accounts succeeded and others failed, were blocked, or remain
  queued. Successful posts are already committed.
- `blocked` — no provider request could progress, usually because a verified X
  session or local budget was unavailable.
- `auth_required` — credentials need local user action.
- `provider_failed` — retryable upstream/client failure; it is not proof of an
  account ban.

`processed_kols` means the scheduler considered those queue items. Use
`successful_kols`, `failed_kols`, `blocked`, and `pending` for the actual result.

## Market maintenance

Admission preview (when reviewing a newly confirmed lead):

```powershell
python _automation\trading_research\trading_cli.py `
  market-symbol-admissions reconcile --as-of auto
```

Daily publication is preview-only unless `--apply` is supplied:

```powershell
python _automation\trading_research\trading_cli.py market-daily-publish `
  --as-of auto --apply --report _runtime\trading\restore-reports\market-daily-publish.json
```

The command builds and validates an isolated candidate, then atomically swaps
the market directory. A failed run leaves the prior data and `as_of` unchanged.
If a run completed the candidate but failed during the final swap, validate it
again and promote it without repeating the provider downloads:

```powershell
python _automation\trading_research\trading_cli.py market-daily-publish `
  --promote-candidate D:\aiworkspace\ai-hub\_runtime\trading\market-live-candidate-YYYYMMDD-HHMMSS `
  --apply --report _runtime\trading\restore-reports\market-daily-publish-repair-promote.json
```

Recovery promotion verifies the manifest digest, all raw/qfq end dates, the
active symbol set, and the research/board table contents before the swap. The
PowerShell task wrapper retains child stdout and stderr logs after any failure
under `_runtime/trading/market-task-logs`, outside the atomically swapped market
directory so open log handles cannot block publication on Windows.

FreeStockDB uses the vendor Windows v0.3.5 client. Its HTTP responses are
MessagePack. The update manager stages and verifies both `data` and `data1`,
removes the vendor `disable` marker before each pass, then swaps both databases
together. Acceptance requires current smoke symbols, at least 90% market
cross-section coverage, and a matching external price sample.

Event research refresh remains manual. Return updates run after the daily market
publication; a return-affecting event amendment is rejected before mutation if
return updates have been disabled operationally.

## Tests

```powershell
python scripts\check_review_bundle.py
python -m unittest _automation.trading_research.tests.test_market_admissions
python -m unittest _automation.trading_research.tests.test_model_budget

Push-Location _automation\trading_research\ui
npm test
npm run build
npm run test:e2e
Pop-Location
```

The full legacy backend suite also contains environment-dependent Hermes skill
contract checks and one historical timestamp assertion. Review their baseline
status separately from regressions in changed modules.

## Recovery

- Code and task rollback snapshots live outside the repository under the local
  repair-backup root.
- Market publication keeps the previous market directory as a rollback target.
- Credentials are never part of a Git or filesystem backup.
- After rollback, verify SQLite integrity, the published market manifest,
  `/api/system/health`, and `#/kols` before resuming tasks.

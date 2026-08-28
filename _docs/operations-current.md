# Current operations

Status date: 2026-08-28.

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
- 19:00 — X evening collection.
- 19:20 — Zhihu evening collection.

No market update, return tracking, event research, or performance recomputation
task is installed in historical mode.

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

## Historical market maintenance

Preview:

```powershell
python _automation\trading_research\trading_cli.py `
  market-symbol-admissions reconcile --as-of 2026-08-25
```

`--apply` is an offline maintenance action. Back up SQLite, market state, code,
and task definitions before applying. Do not run ordinary market sync commands
in historical mode.

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

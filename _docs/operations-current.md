# Current operations

Status date: 2026-08-30.

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
- 09:15 — OCR backlog (unlimited local queue, 50-item batches) and bounded model
  classification.
- 19:00 — X evening collection.
- 19:20 — Zhihu evening collection.
- 17:50 — FreeStockDB validation/update (local D: drive).
- 19:30 — atomic daily market publication through BaoStock and fallbacks.

Historical recommendation reconciliation is a maintenance-only operation. It
uses a candidate runtime and does not re-review drafts already approved or
rejected:

```powershell
python _automation\trading_research\trading_cli.py kol-operational-reconcile `
  --lead-scope unprocessed-history --history-scope known-gaps `
  --model-limit-mode unlimited --approval-gate strict-evidence `
  --apply --report _runtime\trading\restore-reports\kol-operational-reconcile.json
```

Use `--resume <run_id>` after an external provider failure. The command can
approve only evidence that is exact in current text/OCR and mapped to an active
stock in the market master. Non-recommendation mentions remain archive-only.

The daily market publisher is the only market write task. Returns, event
research, and performance recomputation tasks remain intentionally disabled.
After a successful first catch-up the mode is `live` with `as_of` set to the
latest completed trading day; `returns_update_enabled` and
`research_update_enabled` remain `false`.

OCR has no local daily quota. Inspect its durable queue status and error fields
for provider failures; unlimited mode is not a provider-side limit bypass.

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
Regular returns/research refresh calls continue to return HTTP 409 by policy.

## Backup evidence upgrade

Preview or apply only the selected high-value backup material:

```powershell
python _automation\trading_research\trading_cli.py kol-backup-upgrade `
  --source "D:\重装备份\02_C盘项目恢复\aiworkspace\ai-hub" `
  --report _runtime\trading\restore-reports\kol-backup-upgrade.json
```

The command never merges old source code, virtual environments, private config,
or the incomplete backup market warehouse. It copies absent media only and
merges audit logs by canonical JSON hash.

Saved GLM classifications can be made visible to human review without another
model call:

```powershell
python _automation\trading_research\trading_cli.py kol-draft-materialize `
  --model glm-zcode --queue-scope backlog --review-date auto --apply
```

This creates `ready`/`needs_attention` backlog drafts only; it never approves
them or creates formal events.

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

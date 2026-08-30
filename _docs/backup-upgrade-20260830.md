# Backup upgrade record — 2026-08-30

## Source and decision

Source: `D:\重装备份\02_C盘项目恢复\aiworkspace\ai-hub`.

The backup is an older pre-`841aed5` project copy. Current source code, virtual
environments, private configuration, and the incomplete market warehouse are
not merged. Current runtime content remains authoritative.

## Applied material

- 521 previously absent media files were copied for 392 existing posts. Six
  files belonging to four post IDs absent from the current database were
  reported as orphans and skipped.
- Existing media files were never overwritten. Every restored entry has a
  size and SHA-256 digest; current references remain content-hash verified.
- 7,402 backup run-log rows, 1,911 checkpoint revisions, and one event revision
  were merged by canonical JSON hash.
- 67 event source notes and activation timestamps, 35 execution-warning fields,
  and the `002036` security name were reconciled using conservative field-level
  rules. Event thesis, direction, status, and URLs were not replaced.

## GLM classification materialization

Saved `glm-zcode` payloads were materialized into the `backlog` review scope in
batches. The command uses stored validated payloads only; it does not call a
model, approve drafts, or create formal events automatically. The operation is
idempotent by the existing draft source signature.

## X policy

The runtime now records `limit_mode=unlimited` for primary sessions and the
FxTwitter public-post fallback. Local volume and interval gates are disabled by
explicit user choice. Request accounting, batch leases, authentication failure
handling, and real upstream 429 cooling remain active. Unlimited mode provides
no account-safety guarantee.

OCR is also configured as `limit_mode=unlimited`: the local daily item cap no
longer stops processing. Durable 50-item batching, attempt accounting, and
provider/process failure states remain active, and this does not bypass provider
limits.

## Rollback and reports

Each apply creates a consistent database and event-file backup under
`D:\aiworkspace\_kol-repair-backups\backup-upgrade-*`. Preview/apply reports
are kept under `_runtime\trading\restore-reports`. The six orphan media paths
remain recoverable only if their source posts are restored into the database.

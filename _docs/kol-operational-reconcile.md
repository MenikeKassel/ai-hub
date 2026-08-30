# Historical recommendation reconciliation

The `kol-operational-reconcile` maintenance command handles only unprocessed
historical recommendation leads. A `(post_id, symbol)` pair is already handled
when it has an approved or rejected draft, or a formal event. A lead that was
only auto-confirmed for market admission is still eligible for reconciliation.

The command stages a copy of the KOL SQLite/event runtime, resumes the durable
X/Zhihu queues classified as known gaps, materializes saved or newly classified
recommendations, and applies the strict evidence gate. It never fabricates
source text, overwrites an existing full post, or auto-registers themes,
retrospectives, holdings, analysis, or secondhand mentions as events.

```powershell
python _automation\trading_research\trading_cli.py kol-operational-reconcile `
  --lead-scope unprocessed-history --history-scope known-gaps `
  --model-limit-mode unlimited --approval-gate strict-evidence `
  --apply --report _runtime\trading\restore-reports\kol-operational-reconcile.json
```

The model limit is unlimited only for this maintenance catch-up. The worker
mutex, provider authentication, upstream failures, and durable retry state stay
active. `--resume <run_id>` continues an interrupted candidate. A partial
publication is explicitly marked when external X/market work remains queued.

Every apply creates a rollback copy under `D:\aiworkspace\_kol-repair-backups`.
The report records approved, rejected, ignored, retryable, and archive-only
counts, plus event IDs and the exact reason for every non-approval.

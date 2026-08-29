# GLM review entrypoint

Review target: ai-hub 3.1.0, local `main` baseline prepared on 2026-08-29.

## Read in this order

1. `README.md`
2. `_docs/architecture-current.md`
3. `_docs/operations-current.md`
4. `CHANGELOG.md`
5. `CONTRIBUTING.md`
6. `AGENTS.md`

Then inspect the primary implementation areas:

- `_automation/trading_research/market_policy.py`
- `_automation/trading_research/market_admissions.py`
- `_automation/trading_research/model_budget.py`
- `_automation/trading_research/kol_posts.py`
- `_automation/trading_research/trading_cli.py`
- `_automation/trading_research/kol_api.py`
- `_automation/trading_research/morning_pipeline.py`
- `_automation/trading_research/ui/src/pages/System.tsx`

## Review objectives

Prioritize findings in this order:

1. Data-loss or content-overwrite risk.
2. Historical-market write-policy bypasses.
3. X request-budget, session-rotation, retry, or credential leakage defects.
4. Queue idempotency, crash recovery, and cross-process concurrency.
5. API correctness and expensive work on lightweight health routes.
6. Migrations, compatibility, tests, and maintainability.

Do not evaluate trading performance or provide investment recommendations.

## Required invariants to challenge

- Runtime/private data is absent from Git.
- Daily market publication is atomic and source-audited; returns and research
  writes remain blocked by policy.
- Confirming a stock lead cannot publish incomplete market data.
- Existing full post content cannot be replaced by a public/recovery stub.
- X rate limiting cannot be bypassed by switching sessions or fallback sources.
- Health polling cannot issue X, Zhihu, or FxTwitter collection requests.
- Restarting a queue cannot duplicate posts, classifications, admissions, or
  formal events.
- Secrets cannot appear in API responses, logs, docs, tests, or Git history.

## Current evidence and known limitations

- The console is consolidated on 8123. Market mode is live through the latest
  completed close (`2026-08-28`); returns/research refresh writes return 409.
- Published market coverage has 651 complete raw/qfq series; one confirmed
  admission remains pending and four newly discovered symbols await the next
  daily publication.
- X slot state and request usage are persisted; unconfigured slots remain
  disabled rather than copying a legacy credential.
- One X run may become `partial` when the only verified identity reaches its
  90-request cap. Generic per-handle `TwitterAPIError` details need safer status
  preservation.
- OCR and model backlogs are deliberately daily-bounded and resumable.
- Large modules remain a maintainability concern; extracted policy repositories
  are the first step of an incremental split.

Runtime databases and private credentials are not supplied to the reviewer.
Tests must use temporary fixtures and mocks.

## Requested finding format

For every finding, return:

- Severity: P0, P1, P2, or P3.
- Evidence: exact file and line.
- Trigger: minimal scenario that reproduces the issue.
- Impact: data, security, reliability, or maintainability consequence.
- Recommendation: smallest safe fix.
- Verification: test that would fail before and pass after.

Separate confirmed defects from questions and optional refactors. If no defect
is found in a reviewed area, state what was inspected and which invariant was
validated.

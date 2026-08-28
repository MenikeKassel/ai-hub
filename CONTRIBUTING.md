# Contributing

This repository is a local research-audit system. It is not a trading engine.

## Git workflow

- `main` must remain runnable and reviewable.
- Use short branches: `fix/<topic>`, `feat/<topic>`, `refactor/<topic>`, or
  `docs/<topic>`.
- Prefer Conventional Commit subjects: `fix:`, `feat:`, `refactor:`, `test:`,
  `docs:`, `chore:`.
- Keep runtime migrations backward-compatible and idempotent.
- Do not rewrite or delete user runtime data to make a test pass.

## Required boundaries

- Never commit `_runtime`, `_external`, databases, Parquet, media, cookies,
  browser profiles, `.env`, credentials, or local private configuration.
- Never add broker execution, automatic trading, or copy-trading behavior.
- In historical mode, normal market writes must remain blocked. Only explicit
  offline restore/admission maintenance may publish a verified candidate.
- Existing post body, media, raw response, and content hash fields are
  preservation invariants.
- Nitter remains a loopback-only shadow source.

## Review checklist

1. Run `python scripts/check_review_bundle.py`.
2. Compile changed Python modules.
3. Run the smallest relevant backend tests, then the affected full test module.
4. Run `npm test`, `npm run build`, and browser tests for UI changes.
5. Confirm `/api/system/health` stays lightweight and does not contact X or
   Zhihu.
6. Confirm errors and API responses do not contain credentials or cookies.
7. Update `CHANGELOG.md` and current documentation for behavior changes.

## Commit content

Generated build output and test artifacts are not commit content. A change is
complete when source, tests, documentation, and an evidence-backed review note
agree on the same behavior.

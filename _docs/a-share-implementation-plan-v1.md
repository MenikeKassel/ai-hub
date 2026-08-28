# A-Share Research Audit v1 Implementation Plan

Updated: 2026-07-10

This document turns the confirmed blueprint into an executable 14-day plan.
The purpose is continuity: Claude, Codex, or the user should be able to reopen
the repo and know exactly what to do next.

## North Star

Build a beginner loss-prevention and research audit system for A-share
investing.

The system should convert noisy external information into:

1. raw records with source links and state,
2. a controlled stock lead funnel,
3. local data snapshots and validation records,
4. Obsidian audit pages that explain why a judgment exists,
5. weekly reviews that expose mistakes and missing evidence.

It must not become an automatic trading system, KOL copy-trading engine, target
price machine, or full quant platform in v1.

## Current Assets

- Repo: `E:\aiworkspace\ai-hub`
- GitHub remote: `https://github.com/MenikeKassel/ai-hub.git`
- Obsidian vault: `F:\research`
- Hermes user config: `C:\Users\YOUR_USER\.hermes`
- Hermes runtime: `C:\Users\YOUR_USER\AppData\Local\hermes`
- Old quant sandbox: `E:\aiworkspace\lianghua`

Existing Obsidian trading pages should be reused instead of creating a second
parallel trading vault:

```text
F:\research\04_Projects\A股交易研究与审计系统.md
F:\research\04_Projects\股票研究台.md
F:\research\04_Projects\KOL推荐事件表.md
F:\research\04_Projects\交易系统开源工具登记.md
F:\research\04_Projects\历史资料接入计划.md
F:\research\04_Projects\基本面量化系统.md
```

Use them as the v1 shell. Do not rename or move old notes unless the user asks.

## Phase 0: Continuity Baseline

Status: done.

Deliverables:

- `CLAUDE.md`
- `AGENTS.md`
- `_docs/a-share-research-audit-v1.md`
- `_docs/a-share-implementation-plan-v1.md`
- `scripts/doctor.ps1`

Acceptance:

```powershell
powershell -ExecutionPolicy Bypass -File E:\aiworkspace\ai-hub\scripts\doctor.ps1
git -C E:\aiworkspace\ai-hub status --short
```

The doctor script should explain the current environment. Git should be clean
before handing work to another agent.

## Phase 1: Notion State Layer

Goal: make Notion the raw material and state table for trading-related inputs.

Tasks:

1. Confirm the active Notion database id in
   `_automation/hermes-capture/config.yaml`.
2. Add or verify these trading fields in the Notion information database:
   - `stock_code`
   - `stock_name`
   - `investment_workflow_status`
   - `buying_impulse`
   - `trigger_source`
   - `evidence_level`
   - `processing_layer`
   - `review_date`
3. Keep older general capture fields intact:
   - `status`
   - `source_type`
   - `project`
   - `value_score`
   - `obsidian_path`
4. Define allowed `investment_workflow_status` values:
   - `raw`
   - `stock_lead`
   - `impulse_cooling`
   - `candidate_review_needed`
   - `candidate`
   - `deep_research`
   - `rejected`
   - `archived`

Acceptance:

- `/clip` still writes ordinary raw information.
- A manually created stock lead can be represented without target price,
  position, buy/sell advice, AI score, or win-rate fields.
- A buying impulse can be represented with a review date at least 24 hours
  later.

## Phase 2: Hermes Command Layer

Goal: let Feishu input become structured Notion records without producing
investment judgments.

Commands:

```text
/clip <url> [note]
/stock <symbol or name> <why it matters>
/impulse <stock or theme> <why I want to buy now>
/log <text>
```

Tasks:

1. Extend `capture_pipeline.py` parsing for `/stock` and `/impulse`.
2. Map `/stock` to a Notion record with:
   - `investment_workflow_status=stock_lead`
   - `processing_layer=raw`
   - `project=A-share research audit`
3. Map `/impulse` to a Notion record with:
   - `investment_workflow_status=impulse_cooling`
   - `buying_impulse=true`
   - `review_date=today + 1 day`
   - no Obsidian write by default
4. Keep `/log` as daily activity memory, not trading advice.
5. Add tests for parse and payload construction.

Acceptance:

```powershell
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_parse.py
python E:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_capture_doctor.py
```

Manual CLI checks should return JSON and should not create buy/sell decisions:

```powershell
python E:\aiworkspace\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/stock 600900 看到长江电力被多次提到" --source feishu
python E:\aiworkspace\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/impulse 600519 刷到很多人说白酒反转" --source feishu
```

## Phase 3: Obsidian Audit Layer

Goal: make Obsidian hold only processed research, not raw dumping.

Use these existing pages:

- `A股交易研究与审计系统.md`: main workbench and rules.
- `股票研究台.md`: stock funnel and current pool.
- `KOL推荐事件表.md`: event audit table only.
- `基本面量化系统.md`: feature and factor notes.
- `交易系统开源工具登记.md`: external tool registry.
- `历史资料接入计划.md`: backlog for old Notion/X material.

Tasks:

1. Add a v1 section to `A股交易研究与审计系统.md`:
   - goal
   - boundaries
   - stock funnel rules
   - current phase
2. Add stock funnel tables to `股票研究台.md`:
   - lead pool
   - candidate watch pool, max 20
   - deep research pool, max 5
3. Add a stock audit card template:
   - why attention exists
   - core research question
   - positive evidence
   - disconfirming evidence and risks
   - technical/factor state
   - unknowns
   - invalidation condition
   - next review date
   - current conclusion: observe, reject, needs evidence, simulated small position
4. Add a weekly review template under:
   - `F:\research\06_Logs\投资研究复盘\`
5. For a real position, create a holding audit card instead of treating it as a
   system-generated recommendation. The first real position is:
   - `159139`
   - `科创创业人工智能ETF华泰柏瑞`
   - opened on `2026-07-07`
   - entry price `1.460`

Acceptance:

- No raw article dump is moved into a project page.
- Every deep research item can be traced back to Notion source records or data
  snapshots.
- Every audit card has at least one disconfirming or risk item.
- Every real position has a holding audit card with entry facts, missing
  evidence, risk items, invalidation conditions, and next review date.

## Phase 4: Trading Data Layer

Goal: create a small, auditable local data layer before any factor or backtest
work.

Runtime layout:

```text
E:\aiworkspace\ai-hub\_runtime\trading
  raw
    akshare
    baostock
  normalized
    daily_prices
  features
    technical
    basic_fundamental
  audits
    data_quality
  manifests
    data_runs.jsonl
```

Tasks:

1. Decide which Python environment owns trading data dependencies.
2. Install only the v1 dependency set:
   - `pandas`
   - `akshare`
   - `baostock`
   - `pyyaml`
3. Initialize a watchlist CSV.
4. Add an explicit instrument type column before mixing stocks and ETFs.
5. Support ETF daily price retrieval separately from ordinary A-share stock
   retrieval. Do not blindly send ETF codes such as `159139` through a stock-only
   endpoint.
6. Fetch daily prices for the seed watchlist.
7. Write a manifest row for every data run.
8. Cross-check at least 2 ordinary stock symbols with Baostock.
9. Add data quality checks:
   - empty result
   - missing dates
   - duplicate dates
   - non-positive close price
   - suspicious split/adjustment gaps

Acceptance:

```powershell
python E:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py doctor
python E:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py init
python E:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py fetch-prices --start 20240101
python E:\aiworkspace\ai-hub\_automation\trading_research\trading_cli.py verify-baostock --symbols 600900,600519 --start 20240101
```

No factor conclusion is valid unless the underlying data run has a manifest and
passes data quality checks.

ETF note:

- `159139` is now a real-position audit target.
- ETF data support must include fund name, tracking index, liquidity, premium or
  discount, and constituent exposure when possible.
- Until ETF support exists, `159139` can be reviewed manually in Obsidian but
  should not be included in automated stock-only reports.

## Phase 5: Feature Layer

Goal: add simple observable features after the data layer is trustworthy.

v1 features:

- RSI(14)
- MA20
- MA60
- MA250
- 20-day return
- 60-day return
- volatility
- max drawdown
- relative strength versus CSI 300

Tasks:

1. Implement feature generation from normalized daily CSV only.
2. Save output under `_runtime/trading/features/technical`.
3. Record feature run metadata in the manifest.
4. Add an Obsidian-friendly report that states feature values as observations,
   not trading signals.

Acceptance:

- The report says "feature state", not "buy" or "sell".
- Missing benchmark data prevents relative strength output instead of inventing
  a value.
- At least 5 seed symbols can generate feature rows.

## Phase 6: First 14-Day Research Loop

Goal: prove the system improves judgment with a small workload.

Daily minimum:

1. Capture all interesting stock mentions freely.
2. Process at most 3 new stock leads.
3. Review at most 1 candidate.
4. Update at most 1 deep research card.
5. Record one line in the daily or weekly review log.

Weekly minimum:

1. Add 3 to 5 KOL recommendation events only if six required fields exist:
   - KOL
   - platform
   - stock
   - direction
   - reason
   - original link
2. Review pool size:
   - candidate watch pool max 20
   - deep research pool max 5
3. Downgrade or reject weak items.
4. Write one weekly review:
   - what evidence changed
   - which judgment got stronger
   - which judgment got weaker
   - which impulse was avoided
   - what data problem appeared

Day-by-day plan:

```text
Day 1: Notion field alignment.
Day 2: Hermes /stock and /impulse parse + Notion mapping.
Day 3: Obsidian workbench, stock funnel, and audit templates.
Day 4: Trading data runtime init and dependency decision.
Day 5: Fetch seed daily prices and write manifests.
Day 6: Add first feature report for seed symbols.
Day 7: First weekly review.
Day 8-10: Process historical Notion/X leads, max 3 per day.
Day 11-12: Build 1 or 2 complete stock audit cards.
Day 13: Add KOL event audit samples with complete six-field records.
Day 14: Decide whether v2 needs backtesting, more data, or stricter review.
```

## Stock Pool Rules

The user may see many stocks every day. That is expected.

The system should separate attention from commitment:

```text
unlimited incoming mentions
-> lead pool
-> candidate watch pool, max 20
-> deep research pool, max 5
-> buy candidate can be 0
```

Upgrade from lead to candidate requires at least 2 upgrade conditions:

- at least 2 independent sources mention it,
- clear business or industry logic,
- verifiable event,
- abnormal feature state,
- direct relation to a current research question,
- buying impulse that needs loss-prevention audit.

Upgrade from candidate to deep research requires:

- 3 positive evidence items,
- 1 disconfirming or risk item,
- 1 explicit research question.

## Do Not Build In v1

- broker integration
- real-money order placement
- target price or position advice
- full-market scanner
- minute data or Level2 data
- automatic social sentiment index
- complex machine learning model
- automatic promotion from lead to candidate
- automatic promotion from candidate to deep research
- a new Obsidian vault

## Next-Agent Startup

When Claude or Codex resumes, use this order:

```powershell
cd E:\aiworkspace\ai-hub
git status --short
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

Then read:

1. `CLAUDE.md`
2. `_docs/a-share-research-audit-v1.md`
3. `_docs/a-share-implementation-plan-v1.md`
4. `_docs/notion-obsidian-schema.md`

The next implementation step is Phase 1 unless the user explicitly changes the
priority.

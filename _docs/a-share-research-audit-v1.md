# A-Share Research Audit v1

Updated: 2026-07-10

## Purpose

Build a beginner loss-prevention and research audit system for A-share investing.
The system turns external information into traceable, auditable, reviewable
research judgments.

This is not an automatic trading system. It does not produce position advice,
target prices, buy/sell signals, or KOL follow-trade signals.

## Confirmed Principles

1. The system optimizes for research judgment, not saving everything.
2. Notion is the raw material and state layer.
3. Obsidian is the processed knowledge and audit layer.
4. The current main domain is investment and trading research.
5. Other domains stay in collection mode until this domain works.
6. v1 focuses on preventing bad decisions and building judgment.

## Research Method

The main method is falsification-first fundamental audit.

Ask:

- Where can this story be false?
- Did profit become cash?
- Is growth supported by receivables, inventory, capitalization, M&A, subsidies,
  or temporary cycles?
- Did management fulfill prior claims?
- Are there related-party transactions, pledges, reductions, goodwill, financing,
  or abnormal prepayments?
- What third-party evidence confirms or weakens the story?
- What would make this judgment fail?

## Feature Layer

Technical indicators and factors are mathematical price or company features, not
scientific conclusions.

v1 feature set:

- RSI(14)
- MA20 / MA60 / MA250
- turnover and traded value
- 20/60-day return
- volatility and max drawdown
- relative strength versus CSI 300 or an industry benchmark
- simple quality, valuation, growth, and liquidity factors

Funds flow and sentiment are weak state features only. They do not create
conclusions.

## Data Scope

v1 only uses:

- daily OHLCV
- adjusted prices
- CSI 300 or relevant benchmark index
- a small number of basic financial metrics
- explicit stock versus ETF instrument typing

v1 explicitly excludes:

- minute data
- Level2
- complex funds flow
- automatic sentiment index
- full-market factor library
- automated trading interfaces

ETF handling:

- ETFs are allowed as audit targets when the user actually holds or studies
  them.
- ETF records must not be forced through stock-only data logic.
- A real ETF position requires a holding audit card, not a buy/sell
  recommendation.

## Data Truth Source

External APIs are fetch tools, not truth sources.

The local truth source is:

- raw snapshots
- normalized CSV files
- manifest entries
- validation records

Recommended runtime layout:

```text
<AI_HUB_HOME>\ai-hub\_runtime\trading
├─ raw
│  ├─ akshare
│  └─ baostock
├─ normalized
│  └─ daily_prices
├─ features
│  ├─ technical
│  └─ basic_fundamental
├─ audits
│  └─ data_quality
└─ manifests
   └─ data_runs.jsonl
```

v1 uses CSV + manifest. No SQLite/DuckDB/PostgreSQL yet.

## Stock Funnel

```text
Stock lead pool: unlimited
Candidate watch pool: max 20
Deep research pool: max 5
Buy candidate: can be 0
```

### Lead To Candidate

A stock lead upgrades to candidate only when it satisfies at least 2 conditions:

- mentioned by at least 2 independent sources
- has a clear business or industry logic
- has a verifiable event such as order, filing, policy, capacity, price, product,
  or financial result
- has abnormal technical or factor state
- directly relates to a current research question
- triggered a strong buying impulse and needs loss-prevention audit

Pure screenshots, chat hype, single KOL claims, price action alone, or "it will
fly" claims do not qualify.

### Candidate To Deep Research

Upgrade requires:

- 3 positive evidence items
- 1 disconfirming or risk item
- 1 explicit research question

At least one positive evidence item must be hard evidence such as filings,
financial statements, announcements, order data, policy documents, capacity, or
third-party industry data.

## Stock Audit Card

Deep research produces a stock audit card, not a buy/sell recommendation.

Template:

```text
Stock:
Date:
Status:

1. Why am I paying attention?
2. Core research question:
3. Positive evidence:
4. Disconfirming evidence and risks:
5. Technical/factor/market state:
6. What is still unknown?
7. What would strengthen the judgment?
8. What would invalidate the judgment?
9. Next review date:
10. Current conclusion: observe / reject / needs evidence / simulated small position
```

## Buying Impulse Rule

Buying impulse is recorded separately and gets priority over ordinary leads.

Rules:

- record the impulse immediately
- apply a 24-hour cooling-off period
- do not convert same-day impulse into a buy judgment
- after 24 hours, either drop it, keep it as a lead, or manually upgrade based on rules

## Obsidian v1 Pages

```text
<OBSIDIAN_VAULT>\04_Projects\A股防亏与研究审计系统.md
<OBSIDIAN_VAULT>\04_Projects\股票线索池.md
<OBSIDIAN_VAULT>\04_Projects\候选观察池.md
<OBSIDIAN_VAULT>\04_Projects\深度研究池.md
<OBSIDIAN_VAULT>\04_Projects\买入冲动记录.md
<OBSIDIAN_VAULT>\04_Projects\KOL推荐事件审计.md
<OBSIDIAN_VAULT>\06_Logs\投资研究复盘\
```

Stock audit cards are created only for deep research items.

## Notion v1 Fields

Minimum trading-related fields:

- stock code
- stock name
- investment workflow status
- buying impulse checkbox
- trigger source
- evidence level
- processing layer
- review date

Do not add target price, position, buy price, sell price, win rate, AI score,
composite score, or automated recommendation fields in v1.

## Hermes v1 Commands

```text
/clip <url> [note]
/stock <symbol or name> <why it matters>
/impulse <stock or theme> <why I want to buy now>
/log <text>
```

Hermes can identify, save, classify, and mark status. It must not promote leads,
generate buy/sell decisions, or create position advice.

No `/promote` command in v1.

## Implementation Order

Detailed execution plan: `_docs/a-share-implementation-plan-v1.md`.

1. Commit this blueprint and startup docs.
2. Add Notion minimal fields.
3. Implement `/stock`.
4. Implement `/impulse`.
5. Create Obsidian v1 pages and templates.
6. Add trading CSV + manifest data run structure.
7. Only then consider feature calculation such as RSI and moving averages.

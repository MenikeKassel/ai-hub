# Board Mainline RPS v1

## Purpose

This module describes relative strength at the industry and concept-board
level. It is research context, not a stock score, trading signal, position
recommendation, or automatic execution rule.

The implementation borrows the public RPS threshold idea commonly associated
with Tao Doctor, then adds explicit breadth and turnover confirmation. It does
not claim to reproduce any private or complete trading system.

## Data Model

- `board_catalog`: provider-neutral `BKxxxx` board identity, kept separate
  from the six-digit stock/ETF/index master. Eastmoney is preferred; THS codes
  are normalized to the same local `BK` namespace when Eastmoney is blocked.
- `board_daily`: unadjusted board-index OHLC, turnover, breadth, and leader.
- `board_rps`: return windows, cross-sectional RPS, confirmation state, formula
  version, coverage, warnings, and input hash.
- `board_memberships`: dated component snapshots for KOL-event overlays.
- `board_fetch_queue` and `board_runs`: resumable backfill and provider audit.

Raw provider responses live under `_runtime/trading/market/raw/boards`.
Normalized yearly Parquet lives under
`_runtime/trading/market/warehouse/boards`. Runtime data is Git-ignored.

## Formula And State

```text
Return_N = Close_t / Close_(t-N) - 1
RPS_N = 100 * (ascending average rank - 1) / (eligible board count - 1)
```

Windows are 50, 120, and 250 trading sessions. Industry and concept boards are
ranked separately. Ties use average rank.

- `strong_watch`: at least one RPS is 87 or higher.
- `mainline_candidate`: RPS50 >= 87, RPS120 or RPS250 >= 80, breadth >= 60%,
  and turnover is at least its prior 20-session mean.
- `persistent_candidate`: the board met `mainline_candidate` on at least three
  of the latest five sessions.
- `rps_only`: breadth or turnover is unavailable.
- `partial_universe`: effective coverage is below 90%; no mainline label is
  published.

Historical backfill uses the current board catalog and therefore carries a
survivorship and component-change warning.

## Provider Behavior

The Eastmoney adapter is serial, waits 1.5 to 2.5 seconds between calls, retries
finitely, and opens a circuit after three consecutive request failures. The
automatic provider then falls back to the public THS board name/index endpoints,
with its own slower serial limiter. The selected provider is stored on every
raw and normalized row; one response is never merged across providers.
Blocked or stale sources preserve the last valid local snapshot. Empty results
never overwrite history and never create a ranking. THS does not provide the
same stable breadth or membership fields, so those fields remain null and the
RPS-only/partial-universe states stay visible until adequate data exists.

## Operations

```powershell
python trading_cli.py market-board-doctor
python trading_cli.py market-board-sync --as-of YYYY-MM-DD
python trading_cli.py market-board-backfill --resume --days 320 --board-type industry --batch-size 100
python trading_cli.py market-board-backfill --resume --days 320 --board-type concept --batch-size 80
python trading_cli.py market-board-rps --as-of YYYY-MM-DD
```

The workbench route is `#/boards`. The weekday task runs at 20:30. The Sunday
task resumes backfill and refreshes candidate memberships at 11:30.

## References

- AKShare Eastmoney board APIs:
  https://akshare.akfamily.xyz/data/stock/stock.html
- Public RPS method notes:
  https://www.cnblogs.com/sykent/p/16747890.html
- Public monthly-reversal notes:
  https://xstarcd.github.io/wiki/stock/TBS_MonthLineRollback6.0.html
- Eastmoney throttling lessons from `a-stock-data`:
  https://github.com/simonlin1212/a-stock-data

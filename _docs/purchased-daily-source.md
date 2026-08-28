# Purchased daily archive

`D:\a_data\数据更新时间2026.7.31` is a dated, read-only daily snapshot named
`purchased_daily_20260731` inside the KOL audit workbench.

It is not an official runtime provider and it must not overwrite a passing
BaoStock or FreeStockDB series. Its intended uses are:

- fill history before the current canonical Parquet start date;
- cross-check raw OHLC and adjustment factors;
- provide an offline historical source for stock-level research.

The archive contains stock CSV files with raw, forward-adjusted and
backward-adjusted fields. It does not replace ETF `159139`, CSI 300, security
master, financial or announcement sources. The archive has no sufficient
provenance or license metadata, so its outputs remain labelled with the dated
provider name.

## Commands

Run with the trading environment Python:

```powershell
python trading_cli.py market-purchased-daily-doctor `
  --root D:\a_data\数据更新时间2026.7.31 `
  --expected-date 2026-07-31

python trading_cli.py market-purchased-daily-import `
  --root D:\a_data\数据更新时间2026.7.31 `
  --from-research-pool `
  --start 1990-01-01 `
  --end 2026-07-31 `
  --adjustments raw
```

`--from-research-pool` covers formal events, the current watchlist, and
portfolio positions. Use `--from-events` when the import must be strict about
every formal event having a source file. A missing file in the broader pool is
reported as a warning so one absent ETF or index does not hide successful stock
history imports.

The importer only merges rows older than each symbol's existing canonical
series. It writes a runtime manifest and normalised Parquet files, while the
source directory remains untouched. `vol` is converted from lots to shares and
`amount` from thousand yuan to yuan. Turnover and trade status remain missing
when the archive does not provide them; they are never filled with zero.

The archive's precomputed qfq values are anchored to the snapshot date. Use
them only when that snapshot semantics is acceptable. For strict point-in-time
event research, rebuild an event-date-adjusted series from raw prices and the
stored adjustment factor instead of treating current qfq as historical truth.

The doctor performs a fast metadata-only audit of the multi-gigabyte snapshot;
the importer validates every selected row's symbol and date before writing
Parquet. A report with `source_status=stale` must not be presented as a fresh
daily sync.

Do not install or call the unrelated `tinyshare` proxy as part of this source.

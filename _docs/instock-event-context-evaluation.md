# InStock Event-Context Evaluation

Updated: 2026-07-20

## Decision

Use [myhhub/stock](https://github.com/myhhub/stock) as a research reference,
not as a runtime dependency or a replacement for the existing market store.
The useful idea is to describe the market state at the time of a KOL event.
The implementation in this repository is independent Pandas/Numpy code.

## Adopted

- Event-time RSI(14), MACD(12,26,9), ATR(14), five-day volume ratio,
  20-session return, and distance from the 60-session closing high.
- Forward-adjusted prices for technical context only.
- Maximum favorable excursion alongside the existing maximum adverse excursion.
- Versioned, auditable feature snapshots linked to one formal event.

Reference modules reviewed:

- [Indicator calculation](https://github.com/myhhub/stock/blob/master/instock/core/indicator/calculate_indicator.py)
- [Strategy catalog](https://github.com/myhhub/stock/tree/master/instock/core/strategy)
- [CYQ implementation](https://github.com/myhhub/stock/blob/master/instock/core/kline/cyq.py)
- [Forward-return statistics](https://github.com/myhhub/stock/blob/master/instock/core/backtest/rate_stats.py)

## Rejected For v1

- Eastmoney scraping and cookie-dependent endpoints: provider reliability belongs
  in the existing BaoStock/AKShare routing layer.
- MySQL and the bundled web stack: DuckDB, Parquet, FastAPI, and React already
  provide the required local boundaries.
- TA-Lib runtime dependency: the six formulas are small and testable without a
  native binary dependency.
- Automatic trading and strategy signals: they conflict with the research-only
  boundary.
- The forward-return helper as a backtest: it does not model execution timing,
  costs, slippage, benchmark construction, or portfolio mechanics.
- Zero-filling missing indicators: insufficient history remains null and visible.

## Deferred Experiments

CYQ chip distribution and deterministic pattern rules may be evaluated later in
an isolated research module. They must first have fixture-based validation,
documented assumptions, and no effect on KOL ranking or event approval.

The upstream project is Apache-2.0 licensed. No upstream source code is copied
into this implementation.

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

import pandas as pd
from filelock import FileLock, Timeout

from market_data import MARKET_CLOSE_TIME, MARKET_TIMEZONE, MarketStore, now_iso


FORMULA_VERSION = "board-rps-v1"
RANK_FORMULA_VERSION = "board-rank-v1"
BOARD_TYPES = {"industry", "concept"}
LOOKBACKS = (50, 120, 250)


def rank_formula_version(formula_version: str) -> str:
    suffix = formula_version.removeprefix("board-rps-")
    return f"board-rank-{suffix}"


def board_key(board_type: str, board_code: str) -> str:
    if board_type not in BOARD_TYPES:
        raise ValueError(f"unsupported board type: {board_type}")
    code = str(board_code).strip().upper()
    if not code.startswith("BK"):
        raise ValueError(f"invalid board code: {board_code}")
    return f"{board_type}:{code}"


def _number(value: Any) -> float | None:
    if value is None or value is pd.NA:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _hash_payload(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BoardRunResult:
    run_id: str
    operation: str
    status: str
    processed: int
    succeeded: int
    failed: int
    errors: list[str]


class BoardProvider(Protocol):
    name: str

    def fetch_catalog(self, board_type: str) -> pd.DataFrame: ...

    def fetch_history(
        self,
        board_type: str,
        board_name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame: ...

    def fetch_members(self, board_type: str, board_code: str) -> pd.DataFrame: ...


class EastmoneyBoardProvider:
    name = "akshare-eastmoney"

    def __init__(
        self,
        *,
        min_interval: float = 1.5,
        max_interval: float = 2.5,
        max_retries: int = 2,
        failure_threshold: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self.max_interval = max(max_interval, min_interval)
        self.max_retries = max(1, max_retries)
        self.failure_threshold = max(1, failure_threshold)
        self.sleep = sleep
        self._last_call_at = 0.0
        self._consecutive_failures = 0

    def _wait(self) -> None:
        delay = random.uniform(self.min_interval, self.max_interval)
        remaining = delay - (time.monotonic() - self._last_call_at)
        if remaining > 0:
            self.sleep(remaining)

    def _call(self, operation: str, callback: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        if self._consecutive_failures >= self.failure_threshold:
            raise RuntimeError(f"source_blocked: circuit open before {operation}")
        errors: list[str] = []
        for attempt in range(self.max_retries):
            self._wait()
            try:
                frame = callback()
                self._last_call_at = time.monotonic()
                if frame is None or frame.empty:
                    raise RuntimeError("provider returned no rows")
                self._consecutive_failures = 0
                return frame.copy()
            except Exception as exc:
                self._last_call_at = time.monotonic()
                self._consecutive_failures += 1
                errors.append(f"{type(exc).__name__}: {exc}")
                if self._consecutive_failures >= self.failure_threshold:
                    raise RuntimeError(
                        f"source_blocked: {operation} failed: {errors[-1][:1000]}"
                    ) from exc
                if attempt + 1 < self.max_retries:
                    self.sleep(min(8.0, 2.0 ** attempt + random.random()))
        raise RuntimeError(f"{operation} failed: {errors[-1][:1000]}")

    def fetch_catalog(self, board_type: str) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        if board_type == "industry":
            return self._call("industry catalog", ak.stock_board_industry_name_em)
        if board_type == "concept":
            return self._call("concept catalog", ak.stock_board_concept_name_em)
        raise ValueError(f"unsupported board type: {board_type}")

    def fetch_history(
        self,
        board_type: str,
        board_name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        start_value = start.strftime("%Y%m%d")
        end_value = end.strftime("%Y%m%d")
        if board_type == "industry":
            return self._call(
                f"industry history {board_name}",
                lambda: ak.stock_board_industry_hist_em(
                    symbol=board_name,
                    start_date=start_value,
                    end_date=end_value,
                    period="日k",
                    adjust="",
                ),
            )
        if board_type == "concept":
            return self._call(
                f"concept history {board_name}",
                lambda: ak.stock_board_concept_hist_em(
                    symbol=board_name,
                    period="daily",
                    start_date=start_value,
                    end_date=end_value,
                    adjust="",
                ),
            )
        raise ValueError(f"unsupported board type: {board_type}")

    def fetch_members(self, board_type: str, board_code: str) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        if board_type == "industry":
            return self._call(
                f"industry members {board_code}",
                lambda: ak.stock_board_industry_cons_em(symbol=board_code),
            )
        if board_type == "concept":
            return self._call(
                f"concept members {board_code}",
                lambda: ak.stock_board_concept_cons_em(symbol=board_code),
            )
        raise ValueError(f"unsupported board type: {board_type}")


class ThsBoardProvider(EastmoneyBoardProvider):
    """Use Tonghuashun's public board index endpoints when Eastmoney is blocked."""

    name = "akshare-ths"

    def __init__(self, **kwargs: Any):
        kwargs.setdefault("min_interval", 0.5)
        kwargs.setdefault("max_interval", 1.0)
        super().__init__(**kwargs)

    @staticmethod
    def _catalog_code(value: Any) -> str:
        code = str(value).strip().upper()
        return code if code.startswith("BK") else f"BK{code}"

    def fetch_catalog(self, board_type: str) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        if board_type == "industry":
            frame = self._call("industry THS catalog", ak.stock_board_industry_name_ths)
        elif board_type == "concept":
            frame = self._call("concept THS catalog", ak.stock_board_concept_name_ths)
        else:
            raise ValueError(f"unsupported board type: {board_type}")
        normalized = frame.rename(columns={"name": "板块名称", "code": "板块代码"})[
            ["板块名称", "板块代码"]
        ].copy()
        normalized["板块代码"] = normalized["板块代码"].map(self._catalog_code)
        normalized.attrs["provider"] = self.name
        return normalized

    def fetch_history(
        self,
        board_type: str,
        board_name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        start_value = start.strftime("%Y%m%d")
        end_value = end.strftime("%Y%m%d")
        if board_type == "industry":
            callback = lambda: ak.stock_board_industry_index_ths(
                symbol=board_name,
                start_date=start_value,
                end_date=end_value,
            )
        elif board_type == "concept":
            callback = lambda: ak.stock_board_concept_index_ths(
                symbol=board_name,
                start_date=start_value,
                end_date=end_value,
            )
        else:
            raise ValueError(f"unsupported board type: {board_type}")
        frame = self._call(f"{board_type} THS history {board_name}", callback)
        frame.attrs["provider"] = self.name
        return frame

    def fetch_members(self, board_type: str, board_code: str) -> pd.DataFrame:
        raise RuntimeError(
            f"{self.name} does not expose a stable {board_type} membership endpoint for {board_code}"
        )


class FallbackBoardProvider:
    """Try Eastmoney first and fall back to THS without mixing a single response."""

    name = "auto:eastmoney->ths"

    def __init__(self, primary: BoardProvider | None = None, fallback: BoardProvider | None = None):
        self.primary = primary or EastmoneyBoardProvider()
        self.fallback = fallback or ThsBoardProvider()

    def _fetch(self, method: str, *args: Any) -> pd.DataFrame:
        errors: list[str] = []
        for provider in (self.primary, self.fallback):
            try:
                frame = getattr(provider, method)(*args)
                frame.attrs["provider"] = str(frame.attrs.get("provider") or provider.name)
                return frame
            except Exception as exc:
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
        raise RuntimeError("all_board_sources_failed: " + " | ".join(errors)[-3000:])

    def fetch_catalog(self, board_type: str) -> pd.DataFrame:
        return self._fetch("fetch_catalog", board_type)

    def fetch_history(
        self,
        board_type: str,
        board_name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        return self._fetch("fetch_history", board_type, board_name, start, end)

    def fetch_members(self, board_type: str, board_code: str) -> pd.DataFrame:
        return self._fetch("fetch_members", board_type, board_code)


class BoardMainlineStore:
    def __init__(self, market: MarketStore):
        self.market = market
        self.root = market.root
        self.raw_root = market.raw_root / "boards"
        self.warehouse_root = market.warehouse_root / "boards"
        self.job_lock_path = self.root / ".board-job.lock"
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.warehouse_root.mkdir(parents=True, exist_ok=True)

    def _read_formula_version(self) -> str:
        with self._locked_connection() as db:
            values = {
                str(row[0])
                for row in db.execute("SELECT DISTINCT formula_version FROM board_rps").fetchall()
            }
        return "board-rps-v2" if "board-rps-v2" in values else FORMULA_VERSION

    @contextmanager
    def _locked_connection(self) -> Iterator[Any]:
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            yield db

    def _write_raw(self, frame: pd.DataFrame, *, run_id: str, name: str) -> Path:
        directory = self.raw_root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.csv.gz"
        frame.to_csv(path, index=False, encoding="utf-8", compression="gzip")
        return path

    def record_run(
        self,
        result: BoardRunResult,
        *,
        provider: str,
        board_type: str,
        started_at: str,
    ) -> None:
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO board_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    result.run_id,
                    result.operation,
                    provider,
                    board_type,
                    result.status,
                    result.processed,
                    result.succeeded,
                    result.failed,
                    started_at,
                    now_iso(),
                    "；".join(result.errors)[:4000],
                ],
            )

    def save_catalog_snapshot(
        self,
        frame: pd.DataFrame,
        *,
        board_type: str,
        as_of: date,
        provider: str,
        run_id: str,
    ) -> int:
        if board_type not in BOARD_TYPES:
            raise ValueError(f"unsupported board type: {board_type}")
        required = {"板块代码", "板块名称"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError("board catalog missing columns: " + ",".join(sorted(missing)))
        self._write_raw(frame, run_id=run_id, name=f"{board_type}-catalog")
        timestamp = now_iso()
        catalog_rows: list[list[Any]] = []
        daily_rows: list[list[Any]] = []
        queue_rows: list[list[Any]] = []
        for _, source in frame.iterrows():
            code = str(source.get("板块代码") or "").strip().upper()
            name = str(source.get("板块名称") or "").strip()
            close = _number(source.get("最新价"))
            if not code.startswith("BK") or not name:
                continue
            key = board_key(board_type, code)
            payload = {
                "board_key": key,
                "trade_date": as_of.isoformat(),
                "close": close,
                "turnover": _number(source.get("换手率")),
                "up_count": _integer(source.get("上涨家数")),
                "down_count": _integer(source.get("下跌家数")),
                "leader_name": str(source.get("领涨股票") or "").strip(),
                "leader_change": _number(source.get("领涨股票-涨跌幅")),
            }
            catalog_rows.append(
                [key, code, name, board_type, "active", provider, timestamp, timestamp, timestamp]
            )
            if close is not None and close > 0:
                daily_rows.append(
                    [
                        key, as_of.isoformat(), None, None, None, close, None, None,
                        payload["turnover"], payload["up_count"], payload["down_count"],
                        payload["leader_name"], payload["leader_change"], provider, "catalog_snapshot",
                        False, timestamp, _hash_payload(payload),
                    ]
                )
            queue_rows.append(
                [key, 10 if board_type == "industry" else 20, "pending", 0, "", timestamp]
            )
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            db.execute("BEGIN TRANSACTION")
            try:
                if catalog_rows:
                    db.executemany(
                        """
                        INSERT INTO board_catalog VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(board_key) DO UPDATE SET
                            board_code=excluded.board_code,board_name=excluded.board_name,
                            board_type=excluded.board_type,status='active',provider=excluded.provider,
                            last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at
                        """,
                        catalog_rows,
                    )
                if daily_rows:
                    db.executemany(
                        """
                        INSERT INTO board_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(board_key,trade_date) DO UPDATE SET
                            close=excluded.close,
                            turnover=COALESCE(excluded.turnover,board_daily.turnover),
                            up_count=COALESCE(excluded.up_count,board_daily.up_count),
                            down_count=COALESCE(excluded.down_count,board_daily.down_count),
                            leader_name=CASE WHEN excluded.leader_name<>'' THEN excluded.leader_name ELSE board_daily.leader_name END,
                            leader_change=COALESCE(excluded.leader_change,board_daily.leader_change),
                            provider=excluded.provider,fetched_at=excluded.fetched_at,input_hash=excluded.input_hash
                        """,
                        daily_rows,
                    )
                for row in queue_rows:
                    db.execute(
                        """
                        INSERT INTO board_fetch_queue VALUES (?,?,?,?,?,?)
                        ON CONFLICT(board_key) DO NOTHING
                        """,
                        row,
                    )
                db.execute(
                    "UPDATE board_catalog SET status='inactive',updated_at=? WHERE board_type=? AND last_seen_at<>?",
                    [timestamp, board_type, timestamp],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return len(catalog_rows)

    def pending_history(self, limit: int, board_type: str | None = None) -> list[dict[str, Any]]:
        if board_type is not None and board_type not in BOARD_TYPES:
            raise ValueError(f"unsupported board type: {board_type}")
        type_filter = "AND c.board_type=?" if board_type else ""
        parameters: list[Any] = [board_type] if board_type else []
        parameters.append(limit)
        with self._locked_connection() as db:
            cursor = db.execute(
                f"""
                SELECT q.*,c.board_code,c.board_name,c.board_type
                FROM board_fetch_queue q
                JOIN board_catalog c USING(board_key)
                WHERE q.status IN ('pending','failed') AND c.status='active'
                {type_filter}
                ORDER BY q.priority,q.attempts,q.board_key
                LIMIT ?
                """,
                parameters,
            )
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def mark_history(self, key: str, status: str, error: str = "") -> None:
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            db.execute(
                """
                UPDATE board_fetch_queue
                SET status=?,attempts=attempts+1,last_error=?,updated_at=?
                WHERE board_key=?
                """,
                [status, error[:2000], now_iso(), key],
            )

    def save_history(
        self,
        frame: pd.DataFrame,
        *,
        board: dict[str, Any],
        provider: str,
        run_id: str,
        max_sessions: int,
    ) -> int:
        aliases = {
            "日期": "trade_date",
            "开盘": "open",
            "开盘价": "open",
            "最高": "high",
            "最高价": "high",
            "最低": "low",
            "最低价": "low",
            "收盘": "close",
            "收盘价": "close",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover",
        }
        normalized = frame.rename(columns=aliases).copy()
        required = {"trade_date", "open", "high", "low", "close"}
        missing = required - set(normalized.columns)
        if missing:
            raise ValueError("board history missing columns: " + ",".join(sorted(missing)))
        raw_trade_date = normalized["trade_date"]
        numeric_trade_date = pd.to_numeric(raw_trade_date, errors="coerce")
        if (
            numeric_trade_date.notna().mean() >= 0.8
            and not numeric_trade_date.dropna().empty
            and numeric_trade_date.dropna().median() >= 10**11
        ):
            normalized["trade_date"] = pd.to_datetime(
                numeric_trade_date, unit="ms", errors="coerce"
            ).dt.date
        else:
            normalized["trade_date"] = pd.to_datetime(
                raw_trade_date, errors="coerce"
            ).dt.date
        for column in ("open", "high", "low", "close", "volume", "amount", "turnover"):
            if column not in normalized:
                normalized[column] = pd.NA
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized = normalized.dropna(subset=["trade_date", "close"])
        normalized = normalized[normalized["close"] > 0].sort_values("trade_date").tail(max_sessions)
        if normalized.empty:
            raise ValueError("board history has no valid rows")
        self._write_raw(frame, run_id=run_id, name=board["board_key"].replace(":", "-"))
        timestamp = now_iso()
        rows: list[list[Any]] = []
        for _, source in normalized.iterrows():
            payload = {
                "board_key": board["board_key"],
                "trade_date": source["trade_date"].isoformat(),
                "open": _number(source["open"]),
                "high": _number(source["high"]),
                "low": _number(source["low"]),
                "close": _number(source["close"]),
                "volume": _number(source["volume"]),
                "amount": _number(source["amount"]),
                "turnover": _number(source["turnover"]),
            }
            rows.append(
                [
                    board["board_key"], payload["trade_date"], payload["open"], payload["high"],
                    payload["low"], payload["close"], payload["volume"], payload["amount"],
                    payload["turnover"], None, None, "", None, provider, "history",
                    True, timestamp, _hash_payload(payload),
                ]
            )
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            db.executemany(
                """
                INSERT INTO board_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(board_key,trade_date) DO UPDATE SET
                    open=COALESCE(excluded.open,board_daily.open),
                    high=COALESCE(excluded.high,board_daily.high),
                    low=COALESCE(excluded.low,board_daily.low),
                    close=excluded.close,
                    volume=COALESCE(excluded.volume,board_daily.volume),
                    amount=COALESCE(excluded.amount,board_daily.amount),
                    turnover=COALESCE(board_daily.turnover,excluded.turnover),
                    provider=excluded.provider,source_kind=excluded.source_kind,
                    historical_backfill=true,fetched_at=excluded.fetched_at,input_hash=excluded.input_hash
                """,
                rows,
            )
        self._write_board_parquet(board["board_key"])
        return len(rows)

    def _write_board_parquet(self, key: str) -> None:
        with self._locked_connection() as db:
            frame = db.execute(
                "SELECT * FROM board_daily WHERE board_key=? ORDER BY trade_date",
                [key],
            ).df()
        if frame.empty:
            return
        board_type, code = key.split(":", 1)
        years = pd.to_datetime(frame["trade_date"]).dt.year
        for year in sorted(years.unique()):
            output = frame.loc[years == year].copy()
            destination = self.warehouse_root / board_type / code / f"{int(year)}.parquet"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".parquet.tmp")
            output.to_parquet(temporary, index=False, engine="pyarrow")
            os.replace(temporary, destination)

    def save_memberships(
        self,
        frame: pd.DataFrame,
        *,
        key: str,
        snapshot_date: date,
        provider: str,
    ) -> int:
        code_column = "代码" if "代码" in frame.columns else ""
        name_column = "名称" if "名称" in frame.columns else ""
        if not code_column or not name_column:
            raise ValueError("board membership missing code/name columns")
        rows = []
        timestamp = now_iso()
        for _, source in frame.iterrows():
            symbol = str(source[code_column]).strip().zfill(6)
            if not symbol.isdigit() or len(symbol) != 6:
                continue
            rows.append(
                [key, symbol, str(source[name_column]).strip(), snapshot_date.isoformat(), provider, timestamp]
            )
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            db.executemany(
                "INSERT OR REPLACE INTO board_memberships VALUES (?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def compute_rps(
        self,
        *,
        as_of: date | None = None,
        formula_version: str | None = None,
    ) -> dict[str, Any]:
        output_formula_version = formula_version or FORMULA_VERSION
        output_rank_version = rank_formula_version(output_formula_version)
        with self._locked_connection() as db:
            catalog = db.execute(
                "SELECT * FROM board_catalog WHERE status='active' ORDER BY board_type,board_key"
            ).df()
            daily = db.execute(
                "SELECT * FROM board_daily WHERE trade_date<=? ORDER BY trade_date,board_key",
                [(as_of or date.max).isoformat()],
            ).df()
        if catalog.empty or daily.empty:
            return {"ok": False, "computed": 0, "reason": "no_board_data"}
        first_seen_by_key = {
            str(row["board_key"]): pd.to_datetime(row["first_seen_at"], errors="coerce")
            for _, row in catalog.iterrows()
        }
        computed_rows: list[list[Any]] = []
        rank_rows: list[list[Any]] = []
        computed_at = now_iso()
        for kind in sorted(BOARD_TYPES):
            keys = catalog.loc[catalog["board_type"] == kind, "board_key"].tolist()
            if not keys:
                continue
            values = daily[daily["board_key"].isin(keys)].copy()
            values["trade_date"] = pd.to_datetime(values["trade_date"])
            close = values.pivot_table(index="trade_date", columns="board_key", values="close", aggfunc="last")
            close = close.reindex(columns=keys).sort_index()
            returns: dict[int, pd.DataFrame] = {
                window: close / close.shift(window) - 1 for window in LOOKBACKS
            }
            ranks: dict[int, pd.DataFrame] = {}
            ordinal_ranks: dict[int, pd.DataFrame] = {}
            universe_sizes: dict[int, pd.Series] = {}
            for window, frame in returns.items():
                count = frame.notna().sum(axis=1)
                rank = frame.rank(axis=1, method="average", ascending=True, na_option="keep")
                ranks[window] = rank.sub(1).div(count.sub(1).replace(0, pd.NA), axis=0) * 100
                ordinal_ranks[window] = frame.rank(axis=1, method="min", ascending=False, na_option="keep")
                universe_sizes[window] = count
            width = values.set_index(["trade_date", "board_key"])[["up_count", "down_count", "turnover"]]
            width = width[~width.index.duplicated(keep="last")]
            turnover = values.pivot_table(
                index="trade_date", columns="board_key", values="turnover", aggfunc="last"
            ).reindex(index=close.index, columns=keys)
            turnover_average = turnover.shift(1).rolling(20, min_periods=20).mean()
            turnover_ratio = turnover / turnover_average
            type_count = len(keys)
            type_rows: list[dict[str, Any]] = []
            for trade_date in close.index:
                for key in keys:
                    if pd.isna(close.at[trade_date, key]):
                        continue
                    rank_rows.append(
                        [
                            key,
                            trade_date.date().isoformat(),
                            _integer(ordinal_ranks[50].at[trade_date, key]),
                            _integer(ordinal_ranks[120].at[trade_date, key]),
                            _integer(ordinal_ranks[250].at[trade_date, key]),
                            int(universe_sizes[50].get(trade_date, 0)),
                            int(universe_sizes[120].get(trade_date, 0)),
                            int(universe_sizes[250].get(trade_date, 0)),
                            output_rank_version,
                            _hash_payload(
                                {
                                    "board_key": key,
                                    "trade_date": trade_date.date().isoformat(),
                                    "return_50": _number(returns[50].at[trade_date, key]),
                                    "return_120": _number(returns[120].at[trade_date, key]),
                                    "return_250": _number(returns[250].at[trade_date, key]),
                                }
                            ),
                            computed_at,
                        ]
                    )
                    r50 = _number(ranks[50].at[trade_date, key])
                    r120 = _number(ranks[120].at[trade_date, key])
                    r250 = _number(ranks[250].at[trade_date, key])
                    ret50 = _number(returns[50].at[trade_date, key])
                    ret120 = _number(returns[120].at[trade_date, key])
                    ret250 = _number(returns[250].at[trade_date, key])
                    width_row = width.loc[(trade_date, key)] if (trade_date, key) in width.index else None
                    up_count = _number(width_row["up_count"]) if width_row is not None else None
                    down_count = _number(width_row["down_count"]) if width_row is not None else None
                    breadth = (
                        up_count / (up_count + down_count)
                        if up_count is not None and down_count is not None and up_count + down_count > 0
                        else None
                    )
                    volume_ratio = _number(turnover_ratio.at[trade_date, key])
                    size50 = int(universe_sizes[50].get(trade_date, 0))
                    size120 = int(universe_sizes[120].get(trade_date, 0))
                    size250 = int(universe_sizes[250].get(trade_date, 0))
                    coverage = min(size50, max(size120, size250)) / type_count if type_count else 0.0
                    warnings: list[str] = []
                    first_seen = first_seen_by_key.get(key)
                    if pd.notna(first_seen) and trade_date.date() < first_seen.date():
                        warnings.append("current_universe_backfill_bias")
                    if coverage < 0.9:
                        warnings.append("partial_universe")
                    if breadth is None:
                        warnings.append("breadth_unavailable")
                    if volume_ratio is None:
                        warnings.append("turnover_history_insufficient")
                    long_rps = max(value for value in (r120, r250) if value is not None) if any(
                        value is not None for value in (r120, r250)
                    ) else None
                    candidate = bool(
                        coverage >= 0.9
                        and r50 is not None
                        and r50 >= 87
                        and long_rps is not None
                        and long_rps >= 80
                        and breadth is not None
                        and breadth >= 0.60
                        and volume_ratio is not None
                        and volume_ratio >= 1.0
                    )
                    if candidate:
                        status = "mainline_candidate"
                    elif coverage < 0.9:
                        status = "partial_universe"
                    elif breadth is None or volume_ratio is None:
                        status = "rps_only"
                    elif any(value is not None and value >= 87 for value in (r50, r120, r250)):
                        status = "strong_watch"
                    else:
                        status = "neutral"
                    type_rows.append(
                        {
                            "board_key": key,
                            "trade_date": trade_date.date(),
                            "return_50": ret50,
                            "return_120": ret120,
                            "return_250": ret250,
                            "rps_50": r50,
                            "rps_120": r120,
                            "rps_250": r250,
                            "breadth": breadth,
                            "turnover_ratio_20": volume_ratio,
                            "status": status,
                            "candidate": candidate,
                            "universe_size_50": size50,
                            "universe_size_120": size120,
                            "universe_size_250": size250,
                            "coverage_ratio": coverage,
                            "warnings": warnings,
                        }
                    )
            rows_frame = pd.DataFrame(type_rows)
            if rows_frame.empty:
                continue
            rows_frame = rows_frame.sort_values(["board_key", "trade_date"])
            rows_frame["persistent"] = False
            for key, group in rows_frame.groupby("board_key", sort=False):
                persistent = group["candidate"].rolling(5, min_periods=3).sum() >= 3
                rows_frame.loc[group.index, "persistent"] = persistent
            persistent_ready = (
                rows_frame["persistent"]
                & (rows_frame["coverage_ratio"] >= 0.9)
                & rows_frame["breadth"].notna()
                & rows_frame["turnover_ratio_20"].notna()
            )
            rows_frame.loc[persistent_ready, "status"] = "persistent_candidate"
            for _, item in rows_frame.iterrows():
                payload = {
                    column: item[column]
                    for column in (
                        "board_key", "trade_date", "return_50", "return_120", "return_250",
                        "rps_50", "rps_120", "rps_250", "breadth", "turnover_ratio_20",
                        "status", "universe_size_50", "universe_size_120", "universe_size_250",
                        "coverage_ratio",
                    )
                }
                computed_rows.append(
                    [
                        item["board_key"], item["trade_date"].isoformat(),
                        _number(item["return_50"]), _number(item["return_120"]), _number(item["return_250"]),
                        _number(item["rps_50"]), _number(item["rps_120"]), _number(item["rps_250"]),
                        _number(item["breadth"]), _number(item["turnover_ratio_20"]), item["status"],
                        output_formula_version, int(item["universe_size_50"]), int(item["universe_size_120"]),
                        int(item["universe_size_250"]), float(item["coverage_ratio"]),
                        json.dumps(item["warnings"], ensure_ascii=False), _hash_payload(payload), computed_at,
                    ]
                )
        with self.market.lock(timeout=30), self.market.connect(lock=False) as db:
            rps_columns = [
                "board_key", "trade_date", "return_50", "return_120", "return_250",
                "rps_50", "rps_120", "rps_250", "breadth", "turnover_ratio_20", "status",
                "formula_version", "universe_size_50", "universe_size_120", "universe_size_250",
                "coverage_ratio", "warnings_json", "input_hash", "computed_at",
            ]
            rank_columns = [
                "board_key", "trade_date", "rank_50", "rank_120", "rank_250",
                "universe_size_50", "universe_size_120", "universe_size_250", "formula_version",
                "input_hash", "computed_at",
            ]
            rps_frame = pd.DataFrame(computed_rows, columns=rps_columns)
            rank_frame = pd.DataFrame(rank_rows, columns=rank_columns)
            if not rps_frame.empty:
                db.register("_board_rps_rows", rps_frame)
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO board_rps SELECT * FROM _board_rps_rows"
                    )
                finally:
                    db.unregister("_board_rps_rows")
            if not rank_frame.empty:
                db.register("_board_rank_rows", rank_frame)
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO board_rank SELECT * FROM _board_rank_rows"
                    )
                finally:
                    db.unregister("_board_rank_rows")
        return {"ok": True, "computed": len(computed_rows), "formula_version": output_formula_version}

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        if "warnings_json" in value:
            try:
                value["warnings"] = json.loads(value.pop("warnings_json"))
            except (TypeError, json.JSONDecodeError):
                value["warnings"] = []
        for key in ("trade_date", "snapshot_date"):
            if key in value and value[key] is not None:
                value[key] = str(value[key])
        return value

    def list_mainline(
        self,
        *,
        board_type: str = "industry",
        status: str = "",
        query: str = "",
        as_of: date | None = None,
        page: int = 1,
        page_size: int = 100,
        sort_by: str = "rps_50",
        descending: bool = True,
    ) -> dict[str, Any]:
        if board_type not in BOARD_TYPES:
            raise ValueError(f"unsupported board type: {board_type}")
        formula_version = self._read_formula_version()
        sort_columns = {
            "rps_50": "r.rps_50",
            "rps_120": "r.rps_120",
            "rps_250": "r.rps_250",
            "breadth": "r.breadth",
            "turnover_ratio_20": "r.turnover_ratio_20",
            "board_name": "c.board_name",
        }
        order = sort_columns.get(sort_by, "r.rps_50")
        filters = ["c.board_type=?", "c.status='active'"]
        params: list[Any] = [board_type]
        if status:
            filters.append("r.status=?")
            params.append(status)
        if query.strip():
            filters.append("(lower(c.board_name) LIKE ? OR lower(c.board_code) LIKE ?)")
            pattern = f"%{query.strip().lower()}%"
            params.extend([pattern, pattern])
        date_filter = "AND r.trade_date<=?" if as_of else ""
        date_params = [as_of.isoformat()] if as_of else []
        where = " AND ".join(filters)
        page_size = min(max(page_size, 1), 200)
        page = max(page, 1)
        offset = (page - 1) * page_size
        with self._locked_connection() as db:
            total = int(
                db.execute(
                    f"""
                    WITH latest AS (
                        SELECT board_key,MAX(trade_date) trade_date
                        FROM board_rps r WHERE formula_version=? {date_filter}
                        GROUP BY board_key
                    )
                    SELECT COUNT(*)
                    FROM board_catalog c
                    JOIN latest l USING(board_key)
                    JOIN board_rps r ON r.board_key=l.board_key AND r.trade_date=l.trade_date
                        AND r.formula_version=?
                    WHERE {where}
                    """,
                    [formula_version, *date_params, formula_version, *params],
                ).fetchone()[0]
            )
            cursor = db.execute(
                f"""
                WITH latest AS (
                    SELECT board_key,MAX(trade_date) trade_date
                    FROM board_rps r WHERE formula_version=? {date_filter}
                    GROUP BY board_key
                )
                SELECT c.board_code,c.board_name,c.board_type,r.*,
                       d.close,d.turnover,d.up_count,d.down_count,d.leader_name,d.leader_change
                FROM board_catalog c
                JOIN latest l USING(board_key)
                JOIN board_rps r ON r.board_key=l.board_key AND r.trade_date=l.trade_date
                    AND r.formula_version=?
                JOIN board_daily d ON d.board_key=r.board_key AND d.trade_date=r.trade_date
                WHERE {where}
                ORDER BY {order} {"DESC" if descending else "ASC"} NULLS LAST,c.board_name
                LIMIT ? OFFSET ?
                """,
                [formula_version, *date_params, formula_version, *params, page_size, offset],
            )
            columns = [item[0] for item in cursor.description]
            items = [self._decode_row(dict(zip(columns, row))) for row in cursor.fetchall()]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def get_board(self, code: str, board_type: str | None = None) -> dict[str, Any] | None:
        code = code.upper()
        formula_version = self._read_formula_version()
        with self._locked_connection() as db:
            if board_type:
                cursor = db.execute(
                    "SELECT * FROM board_catalog WHERE board_key=?",
                    [board_key(board_type, code)],
                )
            else:
                cursor = db.execute(
                    "SELECT * FROM board_catalog WHERE board_code=? ORDER BY board_type LIMIT 1",
                    [code],
                )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [item[0] for item in cursor.description]
            result = dict(zip(columns, row))
            rps_cursor = db.execute(
                """
                SELECT * FROM board_rps
                WHERE board_key=? AND formula_version=?
                ORDER BY trade_date DESC LIMIT 1
                """,
                [result["board_key"], formula_version],
            )
            rps_row = rps_cursor.fetchone()
            if rps_row:
                rps_columns = [item[0] for item in rps_cursor.description]
                result["latest"] = self._decode_row(dict(zip(rps_columns, rps_row)))
            else:
                result["latest"] = None
            member_cursor = db.execute(
                """
                SELECT symbol,security_name,snapshot_date
                FROM board_memberships
                WHERE board_key=? AND snapshot_date=(
                    SELECT MAX(snapshot_date) FROM board_memberships WHERE board_key=?
                )
                ORDER BY symbol
                """,
                [result["board_key"], result["board_key"]],
            )
            member_columns = [item[0] for item in member_cursor.description]
            result["members"] = [
                self._decode_row(dict(zip(member_columns, member)))
                for member in member_cursor.fetchall()
            ]
        return result

    def series(self, code: str, *, board_type: str | None = None, limit: int | None = 320) -> list[dict[str, Any]]:
        board = self.get_board(code, board_type)
        if board is None:
            return []
        formula_version = self._read_formula_version()
        rank_version = rank_formula_version(formula_version)
        with self._locked_connection() as db:
            limit_clause = " LIMIT ?" if limit is not None else ""
            parameters: list[Any] = [formula_version, rank_version, board["board_key"]]
            if limit is not None:
                parameters.append(max(1, min(limit, 10000)))
            cursor = db.execute(
                f"""
                SELECT d.trade_date,d.open,d.high,d.low,d.close,d.volume,d.amount,d.turnover,
                       d.up_count,d.down_count,d.source_kind,
                       r.rps_50,r.rps_120,r.rps_250,r.breadth,r.turnover_ratio_20,r.status,r.warnings_json,
                       b.rank_50,b.rank_120,b.rank_250,b.universe_size_50,b.universe_size_120,b.universe_size_250
                FROM board_daily d
                LEFT JOIN board_rps r ON r.board_key=d.board_key AND r.trade_date=d.trade_date
                    AND r.formula_version=?
                LEFT JOIN board_rank b ON b.board_key=d.board_key AND b.trade_date=d.trade_date
                    AND b.formula_version=?
                WHERE d.board_key=?
                ORDER BY d.trade_date DESC{limit_clause}
                """,
                parameters,
            )
            columns = [item[0] for item in cursor.description]
            rows = [self._decode_row(dict(zip(columns, row))) for row in cursor.fetchall()]
        return list(reversed(rows))

    def rank_series(
        self,
        code: str,
        *,
        board_type: str | None = None,
        window: int = 50,
        range_name: str = "120",
    ) -> dict[str, Any]:
        if window not in LOOKBACKS:
            raise ValueError(f"unsupported ranking window: {window}")
        if range_name not in {"all", "120", "250"}:
            raise ValueError("range must be all, 120, or 250")
        board = self.get_board(code, board_type)
        if board is None:
            return {
                "window": window,
                "available_from": "",
                "available_to": "",
                "display_from": "",
                "display_to": "",
                "total_point_count": 0,
                "returned_point_count": 0,
                "point_count": 0,
                "truncated": False,
                "points": [],
            }
        rank_column = f"rank_{window}"
        rps_column = f"rps_{window}"
        universe_column = f"universe_size_{window}"
        formula_version = self._read_formula_version()
        rank_version = rank_formula_version(formula_version)
        with self._locked_connection() as db:
            cursor = db.execute(
                f"""
                SELECT r.trade_date,r.{rank_column} AS rank,r.{universe_column} AS universe_size,
                       p.{rps_column} AS rps,p.return_{window} AS period_return,
                       p.status,p.warnings_json
                FROM board_rank r
                LEFT JOIN board_rps p ON p.board_key=r.board_key AND p.trade_date=r.trade_date
                    AND p.formula_version=?
                WHERE r.board_key=? AND r.formula_version=? AND r.{rank_column} IS NOT NULL
                ORDER BY r.trade_date DESC
                """,
                [formula_version, board["board_key"], rank_version],
            )
            columns = [item[0] for item in cursor.description]
            rows = [self._decode_row(dict(zip(columns, row))) for row in cursor.fetchall()]
            if not rows:
                # A database upgraded before board_rank was introduced still
                # has usable board_rps returns. Compute a read-time compatibility
                # view so the chart is not blank until the explicit rebuild runs.
                cursor = db.execute(
                    f"""
                    WITH ranked AS (
                        SELECT p.board_key,p.trade_date,
                               RANK() OVER(
                                   PARTITION BY p.trade_date,c.board_type
                                   ORDER BY p.return_{window} DESC NULLS LAST
                               ) AS rank_value,
                               p.universe_size_{window} AS universe_size,
                               p.rps_{window} AS rps,
                               p.return_{window} AS period_return,
                               p.status,p.warnings_json
                        FROM board_rps p
                        JOIN board_daily d ON d.board_key=p.board_key AND d.trade_date=p.trade_date
                        JOIN board_catalog c ON c.board_key=p.board_key
                        WHERE p.formula_version=? AND d.close IS NOT NULL
                          AND p.return_{window} IS NOT NULL
                    )
                    SELECT trade_date,rank_value AS rank,universe_size,rps,period_return,status,warnings_json
                    FROM ranked
                    WHERE board_key=? AND rank_value IS NOT NULL
                    ORDER BY trade_date DESC
                    """,
                    [formula_version, board["board_key"]],
                )
                columns = [item[0] for item in cursor.description]
                rows = [self._decode_row(dict(zip(columns, row))) for row in cursor.fetchall()]
        rows.reverse()
        all_rows = rows
        if range_name == "all":
            selected_rows = all_rows
        else:
            selected_rows = all_rows[-int(range_name) :]
        return {
            "board_code": board["board_code"],
            "board_name": board["board_name"],
            "board_type": board["board_type"],
            "window": window,
            "available_from": all_rows[0]["trade_date"] if all_rows else "",
            "available_to": all_rows[-1]["trade_date"] if all_rows else "",
            "display_from": selected_rows[0]["trade_date"] if selected_rows else "",
            "display_to": selected_rows[-1]["trade_date"] if selected_rows else "",
            "total_point_count": len(all_rows),
            "returned_point_count": len(selected_rows),
            "point_count": len(selected_rows),
            "truncated": len(selected_rows) < len(all_rows),
            "points": selected_rows,
        }

    def latest_candidate_boards(self, limit: int = 20) -> list[dict[str, Any]]:
        formula_version = self._read_formula_version()
        with self._locked_connection() as db:
            cursor = db.execute(
                """
                WITH latest AS (
                    SELECT board_key,MAX(trade_date) trade_date
                    FROM board_rps WHERE formula_version=? GROUP BY board_key
                )
                SELECT c.board_key,c.board_code,c.board_name,c.board_type,r.status,r.rps_50
                FROM latest l
                JOIN board_rps r ON r.board_key=l.board_key AND r.trade_date=l.trade_date
                    AND r.formula_version=?
                JOIN board_catalog c USING(board_key)
                WHERE r.status IN ('mainline_candidate','persistent_candidate')
                ORDER BY CASE r.status WHEN 'persistent_candidate' THEN 0 ELSE 1 END,r.rps_50 DESC
                LIMIT ?
                """,
                [formula_version, formula_version, limit],
            )
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def health(self) -> dict[str, Any]:
        formula_version = self._read_formula_version()
        with self._locked_connection() as db:
            catalog_rows = db.execute(
                "SELECT board_type,COUNT(*) FROM board_catalog WHERE status='active' GROUP BY board_type"
            ).fetchall()
            latest_rows = db.execute(
                "SELECT board_type,MAX(trade_date) FROM board_catalog c JOIN board_daily d USING(board_key) GROUP BY board_type"
            ).fetchall()
            rps_rows = db.execute(
                """
                SELECT c.board_type,COUNT(*)
                FROM board_catalog c
                JOIN board_rps r USING(board_key)
                WHERE r.formula_version=? AND r.trade_date=(
                    SELECT MAX(r2.trade_date) FROM board_rps r2
                    JOIN board_catalog c2 USING(board_key)
                    WHERE c2.board_type=c.board_type AND r2.formula_version=?
                )
                GROUP BY c.board_type
                """,
                [formula_version, formula_version],
            ).fetchall()
            pending = int(
                db.execute(
                    "SELECT COUNT(*) FROM board_fetch_queue WHERE status IN ('pending','failed')"
                ).fetchone()[0]
            )
            run_cursor = db.execute("SELECT * FROM board_runs ORDER BY started_at DESC LIMIT 1")
            run_row = run_cursor.fetchone()
            latest_run = (
                dict(zip([item[0] for item in run_cursor.description], run_row))
                if run_row else None
            )
        catalog = {str(kind): int(count) for kind, count in catalog_rows}
        latest = {str(kind): str(value or "") for kind, value in latest_rows}
        rps_count = {str(kind): int(count) for kind, count in rps_rows}
        coverage_ratios = {
            kind: (rps_count.get(kind, 0) / count if count else 0.0)
            for kind, count in catalog.items()
        }
        status = "empty"
        if latest_run and latest_run["status"] in {"source_blocked", "failed"}:
            status = latest_run["status"]
        elif pending:
            status = "backfilling"
        elif any(ratio < 0.9 for ratio in coverage_ratios.values()):
            status = "partial_coverage"
        elif sum(catalog.values()) and sum(rps_count.values()):
            status = "ready"
        return {
            "status": status,
            "formula_version": formula_version,
            "catalog_counts": catalog,
            "rps_counts": rps_count,
            "coverage_ratios": coverage_ratios,
            "latest_trade_dates": latest,
            "pending_backfill": pending,
            "latest_run": latest_run,
        }


def sync_board_snapshot(
    store: BoardMainlineStore,
    provider: BoardProvider,
    *,
    as_of: date,
    board_types: Iterable[str] = ("industry", "concept"),
    current_time: datetime | None = None,
) -> BoardRunResult:
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    current = current_time or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    completed_cutoff = min(as_of, current.date())
    if completed_cutoff == current.date() and current.time() < MARKET_CLOSE_TIME:
        completed_cutoff -= timedelta(days=1)
    trade_date = store.market.latest_open_date(completed_cutoff)
    if trade_date is None:
        result = BoardRunResult(
            run_id,
            "sync",
            "failed",
            0,
            0,
            0,
            [
                "trading_calendar_missing: no completed open session on or before "
                f"{completed_cutoff.isoformat()}"
            ],
        )
        store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
        return result
    processed = succeeded = failed = 0
    errors: list[str] = []
    completed_types: list[str] = []
    try:
        with FileLock(str(store.job_lock_path), timeout=0):
            for kind in board_types:
                processed += 1
                try:
                    frame = provider.fetch_catalog(kind)
                    frame_provider = str(frame.attrs.get("provider") or provider.name)
                    store.save_catalog_snapshot(
                        frame,
                        board_type=kind,
                        as_of=trade_date,
                        provider=frame_provider,
                        run_id=run_id,
                    )
                    succeeded += 1
                    completed_types.append(kind)
                except Exception as exc:
                    failed += 1
                    errors.append(f"{kind}: {exc}")
    except Timeout:
        result = BoardRunResult(run_id, "sync", "already_running", 0, 0, 0, [])
        store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
        return result
    status = "completed"
    if failed and succeeded:
        status = "completed_with_errors"
    elif failed:
        status = "source_blocked" if any("source_blocked" in error for error in errors) else "failed"
    result = BoardRunResult(run_id, "sync", status, processed, succeeded, failed, errors)
    store.record_run(
        result,
        provider=provider.name,
        board_type=",".join(completed_types) or "all",
        started_at=started_at,
    )
    return result


def backfill_boards(
    store: BoardMainlineStore,
    provider: BoardProvider,
    *,
    as_of: date,
    days: int = 320,
    batch_size: int = 100,
    board_type: str | None = None,
) -> BoardRunResult:
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    processed = succeeded = failed = 0
    errors: list[str] = []
    try:
        with FileLock(str(store.job_lock_path), timeout=0):
            boards = store.pending_history(batch_size, board_type)
            start = as_of - timedelta(days=max(days * 2, days + 120))
            for board in boards:
                processed += 1
                try:
                    frame = provider.fetch_history(
                        board["board_type"],
                        board["board_name"],
                        start,
                        as_of,
                    )
                    frame_provider = str(frame.attrs.get("provider") or provider.name)
                    store.save_history(
                        frame,
                        board=board,
                        provider=frame_provider,
                        run_id=run_id,
                        max_sessions=days + 1,
                    )
                    store.mark_history(board["board_key"], "completed")
                    succeeded += 1
                except Exception as exc:
                    store.mark_history(board["board_key"], "failed", str(exc))
                    failed += 1
                    errors.append(f"{board['board_key']}: {exc}")
                    if "source_blocked" in str(exc):
                        break
    except Timeout:
        result = BoardRunResult(run_id, "backfill", "already_running", 0, 0, 0, [])
        store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
        return result
    status = "completed"
    if failed and succeeded:
        status = "completed_with_errors"
    elif failed:
        status = "source_blocked" if any("source_blocked" in error for error in errors) else "failed"
    result = BoardRunResult(run_id, "backfill", status, processed, succeeded, failed, errors)
    store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
    return result


def sync_candidate_memberships(
    store: BoardMainlineStore,
    provider: BoardProvider,
    *,
    as_of: date,
    limit: int = 20,
) -> BoardRunResult:
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    processed = succeeded = failed = 0
    errors: list[str] = []
    try:
        with FileLock(str(store.job_lock_path), timeout=0):
            for board in store.latest_candidate_boards(limit):
                processed += 1
                try:
                    frame = provider.fetch_members(board["board_type"], board["board_code"])
                    store._write_raw(
                        frame,
                        run_id=run_id,
                        name=f"members-{board['board_key'].replace(':', '-')}",
                    )
                    store.save_memberships(
                        frame,
                        key=board["board_key"],
                        snapshot_date=as_of,
                        provider=str(frame.attrs.get("provider") or provider.name),
                    )
                    succeeded += 1
                except Exception as exc:
                    failed += 1
                    errors.append(f"{board['board_key']}: {exc}")
                    if "source_blocked" in str(exc):
                        break
    except Timeout:
        result = BoardRunResult(run_id, "memberships", "already_running", 0, 0, 0, [])
        store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
        return result
    status = "completed" if not failed else "completed_with_errors" if succeeded else "failed"
    result = BoardRunResult(run_id, "memberships", status, processed, succeeded, failed, errors)
    store.record_run(result, provider=provider.name, board_type="all", started_at=started_at)
    return result

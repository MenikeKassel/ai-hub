from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    BackgroundTasks,
    FoundationRefreshRequest,
    FreeStockDBUpdateRequest,
    HTTPException,
    INDICATOR_VERSION,
    Instrument,
    InstrumentCreate,
    InstrumentPatch,
    MarketSyncRequest,
    Query,
    StockLeadReviewRequest,
    compute_daily_indicators,
    date,
    datetime,
    json,
    now_iso,
    pd,
    read_published_manifest,
)


def register_routes(services: ApiServices) -> None:
    app = services.app
    assert_market_writes_allowed = services.assert_market_writes_allowed
    config = services.config
    event_store = services.event_store
    extract_leads = services.extract_leads
    market_admissions = services.market_admissions
    market_recovery_mode = services.market_recovery_mode
    market_store = services.market_store
    market_writes_enabled = services.market_writes_enabled
    post_store = services.post_store
    run_foundation_refresh_job = services.run_foundation_refresh_job
    run_freestockdb_update_job = services.run_freestockdb_update_job
    safe_freestockdb_health = services.safe_freestockdb_health
    safe_market_health = services.safe_market_health

    @app.post("/api/stock-leads/extract")
    def extract_stock_lead_queue() -> dict[str, Any]:
        return extract_leads()


    @app.get("/api/stock-leads")
    def list_stock_leads(
        status: str | None = None,
        symbol: str | None = None,
        kol_id: int | None = None,
        post_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return post_store.list_stock_leads(
            status=status,
            symbol=symbol,
            kol_id=kol_id,
            post_id=post_id,
            limit=limit,
            offset=offset,
        )


    @app.get("/api/market/admissions")
    def list_market_admissions(
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            return {
                "items": market_admissions.list(status=status, limit=limit, offset=offset),
                "summary": market_admissions.summary(),
                "published_manifest": read_published_manifest(config.runtime_root / "market"),
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.get("/api/stock-mentions")
    def list_stock_mentions(
        q: str = Query(default="", max_length=200),
        kind: str = Query(default="", max_length=40),
        kol_id: int | None = None,
        symbol: str = Query(default="", pattern=r"^$|^\d{6}$"),
        date_from: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
        date_to: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        result = post_store.query_stock_mentions(
            query=q,
            kind=kind,
            kol_id=kol_id,
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )
        related: dict[tuple[str, str], list[str]] = {}
        for event in event_store.load_events():
            post_id = event.source_note.removeprefix("post:").strip() if event.source_note.startswith("post:") else ""
            related.setdefault((post_id, event.symbol), []).append(event.event_id)
        for item in result["items"]:
            item["related_event_ids"] = related.get((str(item["post_id"]), str(item["symbol"])), [])
        return result


    @app.post("/api/stock-leads/{lead_id}/review")
    def review_stock_lead(lead_id: int, body: StockLeadReviewRequest) -> dict[str, Any]:
        try:
            current = post_store.get_stock_lead(lead_id)
            symbol = body.symbol or current["symbol"]
            instrument_before = None
            writes_enabled = market_writes_enabled()
            if body.action == "confirmed":
                if current["status"] == "confirmed":
                    if body.symbol is not None and body.symbol != current["symbol"]:
                        raise ValueError("已确认线索不能直接改代码，请先退回待确认")
                    if not writes_enabled:
                        market_admissions.queue_confirmed_symbols(
                            [symbol],
                            as_of=str(market_recovery_mode().get("as_of") or ""),
                        )
                        return {**current, "market_admission_status": "queued"}
                    return current
                if current["status"] != "pending":
                    raise ValueError("只有待确认线索可以晋升为确认状态")
                if writes_enabled:
                    instrument_before = market_store.get_instrument(symbol)
                    if instrument_before is None:
                        raise ValueError(f"{symbol} 不在证券主数据中，请先刷新主数据或修正代码")
            reviewed = post_store.review_stock_lead(
                lead_id,
                body.action,
                body.note,
                symbol=body.symbol,
                security_name=body.security_name,
            )
            if body.action == "confirmed":
                if not writes_enabled:
                    market_admissions.queue_confirmed_symbols(
                        [symbol],
                        as_of=str(market_recovery_mode().get("as_of") or ""),
                    )
                    return {**reviewed, "market_admission_status": "queued"}
                try:
                    mentioned = datetime.fromisoformat(str(current["posted_at"]).replace("Z", "+00:00")).date()
                except ValueError:
                    mentioned = date.today()
                try:
                    market_store.touch_mention(symbol, mentioned)
                    market_store.enqueue_sync(symbol, reason=f"kol_lead:{current['post_id']}")
                except Exception:
                    post_store.review_stock_lead(
                        lead_id,
                        "pending",
                        "市场数据排队失败，确认操作已回滚",
                        symbol=current["symbol"],
                        security_name=current["security_name"],
                    )
                    if instrument_before is not None:
                        market_store.restore_research_state(
                            symbol,
                            lifecycle=instrument_before["lifecycle"],
                            last_mentioned_at=instrument_before["last_mentioned_at"],
                        )
                    raise
            return reviewed
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"market queue failed: {exc}") from exc


    @app.get("/api/instruments")
    def list_instruments(lifecycle: str | None = None) -> list[dict[str, Any]]:
        return market_store.list_instruments(lifecycle)


    @app.post("/api/instruments", status_code=201)
    def create_instrument(body: InstrumentCreate) -> dict[str, Any]:
        assert_market_writes_allowed()
        return market_store.upsert_instrument(Instrument(**body.model_dump()))


    @app.patch("/api/instruments/{symbol}")
    def patch_instrument(symbol: str, body: InstrumentPatch) -> dict[str, Any]:
        assert_market_writes_allowed()
        current = market_store.get_instrument(symbol)
        if current is None:
            raise HTTPException(404, "instrument not found")
        changes = body.model_dump(exclude_none=True)
        return market_store.upsert_instrument(
            Instrument(
                symbol=symbol,
                name=changes.get("name", current["name"]),
                instrument_type=current["instrument_type"],
                exchange=current["exchange"],
                status=changes.get("status", current["status"]),
                list_date=current["list_date"],
                lifecycle=changes.get("lifecycle", current["lifecycle"]),
                source=current["source"],
                first_seen_at=current["first_seen_at"],
                last_mentioned_at=current["last_mentioned_at"],
            )
        )


    @app.post("/api/market/sync", status_code=202)
    def enqueue_market_sync(body: MarketSyncRequest) -> dict[str, Any]:
        assert_market_writes_allowed()
        symbols = body.symbols or [
            item["symbol"]
            for item in market_store.list_instruments()
            if item["lifecycle"] in {"pinned", "tracking"}
        ]
        queued: list[str] = []
        for symbol in symbols:
            try:
                market_store.enqueue_sync(
                    symbol,
                    start=body.start,
                    end=body.end,
                    priority=20,
                    reason="ui_manual",
                )
                queued.append(symbol)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "queued_symbols": queued}


    @app.post("/api/market/foundation-refresh", status_code=202)
    def refresh_foundation(
        body: FoundationRefreshRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        assert_market_writes_allowed()
        state_path = config.runtime_root / "market" / "foundation-refresh.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            current = {}
        if current.get("status") == "running":
            return {"ok": True, "status": "already_running", "state": current}
        state_path.write_text(
            json.dumps(
                {"status": "running", "as_of": body.as_of, "started_at": now_iso()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        background_tasks.add_task(run_foundation_refresh_job, body.as_of, body.notify, state_path)
        return {"ok": True, "status": "triggered", "as_of": body.as_of}


    @app.get("/api/market/foundation-refresh")
    def foundation_refresh_status() -> dict[str, Any]:
        state_path = config.runtime_root / "market" / "foundation-refresh.json"
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"status": "idle"}


    @app.get("/api/market/health")
    def market_health() -> dict[str, Any]:
        return {
            **safe_market_health(),
            "freestockdb": safe_freestockdb_health(),
            "coverage": market_store.get_coverage(),
            "issues": market_store.quality_issues(),
            "runs": market_store.recent_runs(),
            "queue": market_store.pending_sync(),
        }


    @app.get("/api/market/providers/freestockdb")
    def freestockdb_health() -> dict[str, Any]:
        return safe_freestockdb_health(force=True)


    @app.post("/api/market/providers/freestockdb/update", status_code=202)
    def update_freestockdb(
        body: FreeStockDBUpdateRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        assert_market_writes_allowed()
        current = safe_freestockdb_health(force=True)
        if current.get("port_conflict"):
            raise HTTPException(409, "FreeStockDB port conflict")
        if not current.get("update_ready"):
            failed = [
                name
                for name, passed in current.get("checks", {}).items()
                if passed is False
                and name not in {"service", "provider", "freshness"}
            ]
            raise HTTPException(
                409,
                "FreeStockDB update preflight failed: " + ",".join(failed),
            )
        background_tasks.add_task(run_freestockdb_update_job, body.dry_run)
        return {"ok": True, "status": "queued", "dry_run": body.dry_run}


    @app.get("/api/market/instruments/{symbol}/coverage")
    def instrument_coverage(symbol: str) -> list[dict[str, Any]]:
        if market_store.get_instrument(symbol) is None:
            raise HTTPException(404, "instrument not found")
        return market_store.get_coverage(symbol)


    @app.get("/api/market/instruments/{symbol}/daily")
    def instrument_daily(
        symbol: str,
        adjustment: str = "raw",
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        if adjustment not in {"raw", "qfq", "hfq"}:
            raise HTTPException(422, "invalid adjustment")
        frame = market_store.read_daily(symbol, adjustment=adjustment)
        if frame.empty:
            return []
        if start is not None:
            frame = frame[frame["trade_date"] >= start]
        if end is not None:
            frame = frame[frame["trade_date"] <= end]
        frame = frame.where(frame.notna(), None)
        return frame.to_dict(orient="records")


    @app.get("/api/market/daily/{symbol}/indicators")
    def market_daily_indicators(
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        frame = compute_daily_indicators(
            market_store.read_daily(symbol, adjustment="qfq")
        )
        available_from = (
            frame.iloc[0]["trade_date"].isoformat() if not frame.empty else ""
        )
        available_to = (
            frame.iloc[-1]["trade_date"].isoformat() if not frame.empty else ""
        )
        if start is not None and not frame.empty:
            frame = frame[frame["trade_date"] >= start]
        if end is not None and not frame.empty:
            frame = frame[frame["trade_date"] <= end]
        rows: list[dict[str, Any]] = []
        for raw_row in frame.to_dict(orient="records"):
            row: dict[str, Any] = {}
            for key, raw_value in raw_row.items():
                if pd.isna(raw_value):
                    row[key] = None
                elif isinstance(raw_value, date):
                    row[key] = raw_value.isoformat()
                elif hasattr(raw_value, "item"):
                    row[key] = raw_value.item()
                else:
                    row[key] = raw_value
            rows.append(row)
        return {
            "symbol": symbol,
            "adjustment": "qfq",
            "formula_version": INDICATOR_VERSION,
            "available_from": available_from,
            "available_to": available_to,
            "row_count": len(rows),
            "rows": rows,
        }


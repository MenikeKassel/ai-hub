from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    HTTPException,
    PerformanceRefreshRequest,
    Query,
    date,
)


def register_routes(services: ApiServices) -> None:
    app = services.app
    assert_returns_updates_allowed = services.assert_returns_updates_allowed
    performance = services.performance

    @app.get("/api/kol-leaderboard")
    def kol_leaderboard() -> dict[str, Any]:
        result = performance.compute(as_of=date.today(), window="all", horizon="1W", primary_only=True)
        rows: list[dict[str, Any]] = []
        for item in result["rows"]:
            horizons: dict[str, Any] = {}
            for horizon, metrics in item["horizons"].items():
                value = dict(metrics)
                value.setdefault("samples", value.get("batch_count", 0))
                value.setdefault("median_adverse", value.get("median_mae"))
                horizons[horizon] = value
            rows.append({
                "kol_name": item["kol_name"],
                "kol_key": item["kol_key"],
                "kol_id": item["kol_id"],
                "kol_handle": item["kol_handle"],
                "platform": item["platform"],
                "tier": item["tier"],
                "rank": item["rank"],
                "rank_horizon": item["rank_horizon"],
                "score": None,
                "event_count": item["horizons"]["1W"]["event_count"],
                "long_event_count": item["horizons"]["1W"]["long_event_count"],
                "short_event_count": item["horizons"]["1W"]["short_event_count"],
                "executable_long_event_count": item["horizons"]["1W"]["executable_long_event_count"],
                "audit_event_count": item["horizons"]["1W"]["audit_event_count"],
                "executable_event_count": item["horizons"]["1W"]["event_count"],
                "horizons": horizons,
            })
        return {
            "policy": {
                "primary_metric": "platform-separated median long-only batch excess return",
                "batch_weighting": "same-post stocks are equal-weighted into one batch",
                "score": None,
                "direction_policy": "short events are retained for audit but excluded from A-share return statistics",
                "sample_policy": "small long-only samples are shown but never formally ranked",
                "counting_policy": "long_event_count counts the long events in the return sample; short_event_count counts excluded short events (audit only, independent of window/horizon); executable_long_event_count counts long events free of primary execution warnings; audit_event_count = long_event_count + short_event_count",
            },
            "as_of": result["as_of"],
            "rows": rows,
        }


    @app.get("/api/kol-performance")
    def kol_performance(
        platform: str | None = None,
        window: str = Query(default="all", pattern="^(7|30|90|all)$"),
        horizon: str = Query(default="1W", pattern="^(1W|1M|3M|6M)$"),
        as_of: str | None = None,
    ) -> dict[str, Any]:
        try:
            effective_date = date.fromisoformat(as_of) if as_of else date.today()
            return performance.compute(
                as_of=effective_date,
                platform=platform,
                window=window,
                horizon=horizon,
                primary_only=True,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.get("/api/kol-performance/{kol_key}")
    def kol_performance_detail(kol_key: str, as_of: str | None = None) -> dict[str, Any]:
        try:
            effective_date = date.fromisoformat(as_of) if as_of else date.today()
            result = performance.compute(as_of=effective_date, platform="all", window="all", horizon="1W", primary_only=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        row = next((item for item in result["rows"] if item["kol_key"] == kol_key), None)
        if row is None:
            raise HTTPException(404, "KOL performance identity not found")
        return {
            "version": result["version"],
            "as_of": result["as_of"],
            "row": row,
            "series": {
                horizon: performance.store.series(kol_key, horizon=horizon, window_name="all")
                for horizon in ("1W", "1M", "3M", "6M")
            },
        }


    @app.get("/api/kol-performance/{kol_key}/series")
    def kol_performance_series(
        kol_key: str,
        horizon: str = Query(default="1W", pattern="^(1W|1M|3M|6M)$"),
        range: str = Query(default="all", pattern="^(7|30|90|all)$"),
    ) -> dict[str, Any]:
        points = performance.store.series(kol_key, horizon=horizon, window_name="all")
        return {
            "kol_key": kol_key,
            "horizon": horizon,
            "range": range,
            "points": points,
            "point_count": len(points),
        }


    @app.post("/api/kol-performance/refresh")
    def refresh_kol_performance(body: PerformanceRefreshRequest | None = None) -> dict[str, Any]:
        assert_returns_updates_allowed()
        try:
            effective_date = date.fromisoformat(body.as_of) if body and body.as_of else date.today()
            performance.migrate_event_identities()
            result = performance.refresh(as_of=effective_date)
            return {
                "ok": True,
                "as_of": result["as_of"],
                "run_id": result["run_id"],
                "snapshots_inserted": result["snapshots_inserted"],
                "coverage": result["coverage"],
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


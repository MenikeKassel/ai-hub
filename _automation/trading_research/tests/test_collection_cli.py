from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from filelock import Timeout as FileLockTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli_commands.collection import kol_post_fetch  # noqa: E402
from kol_sources.repository import KolPostStore  # noqa: E402
import trading_cli  # noqa: E402


class CollectionCliTests(unittest.TestCase):
    def test_market_lock_defers_leads_without_losing_completed_fetch(self) -> None:
        store = SimpleNamespace(codex_failure_streak=lambda: 0)
        result = SimpleNamespace(run_id="fetch-1", successful_kols=1, failed_kols=0)

        def market_locked(_store):
            raise FileLockTimeout("market.lock")

        context = SimpleNamespace(
            _post_store=lambda: store,
            _post_provider=lambda *args, **kwargs: object(),
            _zhihu_provider=lambda: object(),
            _douyin_provider=lambda: object(),
            _classification_aliases=lambda: {},
            RuleClassifier=lambda aliases: object(),
            run_post_fetch=lambda *args, **kwargs: result,
            _extract_leads_to_market=market_locked,
            _fallback_mode=lambda: "shadow",
            json=json,
            date=date,
        )
        args = argparse.Namespace(
            backfill=1,
            handles="",
            as_of="2026-09-26",
            platform="zhihu",
            provider="auto",
            batch_key="test-lock",
            dry_run=False,
            skip_classify=True,
            skip_leads=False,
            fresh_first_page=False,
            notify=False,
            alerts_only=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kol_post_fetch(context, args)
        payload = json.loads(output.getvalue())
        self.assertEqual(1, payload["successful_kols"])
        self.assertEqual(
            {"deferred": True, "reason": "market_locked", "retry": "next_fetch"},
            payload["stock_leads"],
        )

    def test_transient_zhihu_failure_cools_down_and_archive_reports_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = store.add_kol("Example", "example", platform="Zhihu")
            key = "morning:old:zhihu"
            store.prepare_fetch_queue(key, [store.get_kol(kol_id)], requested_count=50)
            store.defer_fetch_queue_item(
                key,
                kol_id,
                error_code="provider_transient",
                error="Zhihu code 10003",
                cooldown_seconds=300,
            )
            with store.connect() as db:
                row = db.execute(
                    "SELECT state,not_before FROM fetch_queue WHERE batch_key=?",
                    (key,),
                ).fetchone()
                self.assertEqual("cooldown", row["state"])
                remaining = datetime.fromisoformat(row["not_before"]) - datetime.now().astimezone()
                self.assertGreaterEqual(remaining, timedelta(minutes=4))
                self.assertLessEqual(remaining, timedelta(minutes=6))
                db.execute(
                    "UPDATE fetch_queue SET updated_at=? WHERE batch_key=?",
                    ("2026-01-01T00:00:00+08:00", key),
                )
            cutoff = datetime.now().astimezone()
            self.assertEqual([key], store.archive_legacy_fetch_batches(cutoff))
            self.assertEqual([], store.archive_legacy_fetch_batches(cutoff))

    def test_market_lock_replays_confirmed_symbols_after_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = KolPostStore(root / "posts.db", root / "media")
            market = SimpleNamespace(
                instrument_map=lambda: {"600000": {"name": "Example"}},
                touch_mention=mock.Mock(side_effect=FileLockTimeout("market.lock")),
                enqueue_sync=mock.Mock(),
            )
            first = SimpleNamespace(
                processed_posts=1,
                created=1,
                updated=0,
                confirmed_symbols=["600000"],
                failed=0,
            )
            second = SimpleNamespace(
                processed_posts=0,
                created=0,
                updated=0,
                confirmed_symbols=[],
                failed=0,
            )
            with (
                mock.patch.object(trading_cli, "_market_store", return_value=market),
                mock.patch.object(trading_cli, "_market_writes_enabled", return_value=True),
                mock.patch.object(trading_cli, "_seed_market_instruments"),
                mock.patch.object(trading_cli, "load_stock_aliases", return_value={}),
                mock.patch.object(trading_cli, "reconcile_exact_stock_leads", return_value=[]),
                mock.patch.object(trading_cli, "extract_stock_leads", side_effect=[first, second]),
                mock.patch.object(
                    store,
                    "list_stock_leads",
                    return_value=[{"posted_at": "2026-09-26T00:00:00+08:00", "post_id": "post-1"}],
                ),
            ):
                with self.assertRaises(FileLockTimeout):
                    trading_cli._extract_leads_to_market(store)
                self.assertEqual(["600000"], store.list_market_lead_replay())
                market.touch_mention.side_effect = None
                payload = trading_cli._extract_leads_to_market(store)
            self.assertEqual(1, payload["replayed_symbol_count"])
            self.assertEqual(1, payload["queued_symbol_count"])
            self.assertEqual([], store.list_market_lead_replay())
            market.enqueue_sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()

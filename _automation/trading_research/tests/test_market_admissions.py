from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import KolPostStore, normalise_twitter_post  # noqa: E402
from market_admissions import (  # noqa: E402
    MarketAdmissionRepository,
    read_published_manifest,
    write_published_manifest,
)


class MarketAdmissionTests(unittest.TestCase):
    def make_store(self, root: Path) -> KolPostStore:
        store = KolPostStore(root / "posts.db", root / "media")
        kol_id, _ = store.add_kol("fixture", "fixture")
        kol = store.get_kol(kol_id)
        assert kol is not None
        for index, symbol in enumerate(("600900", "600519", "000001"), start=1):
            post = normalise_twitter_post(
                {
                    "id": f"209900000000000000{index}",
                    "text": f"关注 {symbol}",
                    "url": f"https://x.com/fixture/status/209900000000000000{index}",
                    "author": {"screenName": "fixture", "name": "fixture"},
                    "createdAtISO": "2026-08-25T01:00:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            store.upsert_post(post)
            store.upsert_stock_lead(
                {
                    "post_id": post.post_id,
                    "kol_id": kol_id,
                    "symbol": symbol,
                    "security_name": symbol,
                    "extraction_method": "exact_code",
                    "confidence": 1,
                    "status": "confirmed",
                }
            )
        return store

    def test_confirmed_symbols_are_published_only_when_baseline_or_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            repository = MarketAdmissionRepository(store)
            result = repository.reconcile_confirmed(
                as_of="2026-08-25",
                baseline_symbols={"600900"},
                complete_symbols={"600900", "600519"},
                raw_end_dates={"600900": "2026-08-25", "600519": "2026-08-25"},
                qfq_end_dates={"600900": "2026-08-25", "600519": "2026-08-25"},
                apply=True,
            )

            self.assertEqual(2, result["published"])
            self.assertEqual(1, result["pending"])
            self.assertEqual("pending", repository.list(status="pending")[0]["status"])
            self.assertEqual(3, repository.summary()["total"])

    def test_queue_is_idempotent_and_manifest_has_stable_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.make_store(root)
            repository = MarketAdmissionRepository(store)
            first = repository.queue_confirmed_symbols(["600900", "600900"], as_of="2026-08-25")
            second = repository.queue_confirmed_symbols(["600900"], as_of="2026-08-25")
            first_manifest = write_published_manifest(
                root / "market",
                as_of="2026-08-25",
                baseline_symbols=["600900"],
                extension_symbols=["600519"],
                source="fixture",
            )
            second_manifest = write_published_manifest(
                root / "market",
                as_of="2026-08-25",
                baseline_symbols=["600900"],
                extension_symbols=["600519"],
                source="fixture",
            )

            self.assertEqual(["600900"], first)
            self.assertEqual(["600900"], second)
            self.assertEqual(1, len(repository.list()))
            self.assertEqual(first_manifest["symbols_sha256"], second_manifest["symbols_sha256"])
            self.assertEqual(2, read_published_manifest(root / "market")["published_count"])


if __name__ == "__main__":
    unittest.main()

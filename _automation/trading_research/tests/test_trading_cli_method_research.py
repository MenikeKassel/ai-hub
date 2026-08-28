from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_cli import _run_method_ai_batches  # noqa: E402


class FakeResearchService:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def interpret_pending(self, *, event_ids, max_items):
        values = list(event_ids)
        self.calls.append((values, max_items))
        if len(self.calls) == 1:
            return {
                "ok": False,
                "processed": 3,
                "created": [],
                "failed": [
                    {"event_id": "A"},
                    {"event_id": "B"},
                    {"event_id": "C"},
                ],
                "skipped": [],
                "errors": [
                    {
                        "error": "batch validation failed",
                        "event_ids": ["A", "B", "C"],
                    }
                ],
            }
        event_id = values[0]
        if event_id == "B":
            return {
                "ok": False,
                "processed": 1,
                "created": [],
                "failed": [{"event_id": event_id}],
                "skipped": [],
                "errors": [
                    {"error": "single validation failed", "event_ids": [event_id]}
                ],
            }
        return {
            "ok": True,
            "processed": 1,
            "created": [{"event_id": event_id}],
            "failed": [],
            "skipped": [],
            "errors": [],
        }


class PartialSaveResearchService:
    def __init__(self) -> None:
        self.calls = 0

    def interpret_pending(self, *, event_ids, max_items):
        self.calls += 1
        if self.calls == 1:
            return {
                "ok": False,
                "processed": 2,
                "created": [{"event_id": "A"}],
                "failed": [],
                "skipped": [],
                "errors": [{"event_id": "B", "error": "database write failed"}],
            }
        self.asserted_event_ids = list(event_ids)
        return {
            "ok": True,
            "processed": 1,
            "created": [{"event_id": "B"}],
            "failed": [],
            "skipped": [],
            "errors": [],
        }


class MethodResearchBatchTests(unittest.TestCase):
    def test_isolates_batch_validation_failures_by_event(self) -> None:
        result = _run_method_ai_batches(
            FakeResearchService(),
            event_ids=["A", "B", "C"],
            max_events=0,
        )

        self.assertEqual(3, result["processed"])
        self.assertEqual(["A", "C"], [item["event_id"] for item in result["created"]])
        self.assertEqual(["B"], result["recovered_batches"][0]["failed"])
        self.assertEqual(["A", "C"], result["recovered_batches"][0]["recovered"])
        self.assertFalse(result["ok"])
        self.assertEqual(
            ["single validation failed"],
            [item["error"] for item in result["errors"]],
        )

    def test_does_not_retry_events_already_saved_from_a_partial_batch(self) -> None:
        service = PartialSaveResearchService()

        result = _run_method_ai_batches(
            service,
            event_ids=["A", "B"],
            max_events=0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(["B"], service.asserted_event_ids)
        self.assertEqual(["A", "B"], [item["event_id"] for item in result["created"]])


if __name__ == "__main__":
    unittest.main()

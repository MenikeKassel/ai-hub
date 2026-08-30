from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_operational_reconcile import OperationalReconciler  # noqa: E402


def _candidate(draft_id: int, security_name: str, thesis: str) -> dict:
    return {
        "draft": {
            "id": draft_id,
            "symbol": "301217",
            "security_name": security_name,
            "confidence": 0.8,
            "thesis": thesis,
            "evidence_spans": [f"{security_name}，资金建仓中"],
        },
        "post": {
            "post_id": "2087109651112333632",
            "url": "https://x.com/example/status/2087109651112333632",
            "text": "铜冠铜箔，资金建仓中",
            "article_text": "",
            "quoted_text": "",
            "ocr_text": "",
        },
        "instrument": {"name": "铜冠铜箔"},
    }


def test_deduplicate_prefers_the_market_master_identity() -> None:
    reconciler = object.__new__(OperationalReconciler)
    selected, rejected = reconciler._deduplicate(
        [_candidate(1, "普冉股份", "并未有效反弹"), _candidate(2, "铜冠铜箔", "资金建仓中")],
        {"301217": {"name": "铜冠铜箔"}},
    )

    assert [item["draft"]["id"] for item in selected] == [2]
    assert rejected == [
        {
            "draft_id": 1,
            "post_id": "2087109651112333632",
            "symbol": "301217",
            "reasons": ["duplicate_noncanonical"],
        }
    ]

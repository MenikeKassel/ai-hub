# -*- coding: utf-8 -*-
"""zhihu_collection_monitor 的离线单元测试（不需要浏览器、不需要网络）。

运行：python tests/test_zhihu_collection_monitor.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhihu_collection_monitor import (  # noqa: E402
    HISTORY_KEEP,
    build_baseline_message,
    build_change_report,
    diff_items,
    load_previous,
    normalize_item,
    prune_history,
    save_snapshot,
    scan_sanity_error,
    type_cn,
)


def _item(key, title="T", author="A", url="u", deleted=False):
    item_type, _, item_id = key.partition(":")
    return {"key": key, "type": item_type, "id": item_id, "title": title,
            "author": author, "url": url, "is_deleted": deleted}


def test_normalize_item_answer():
    raw = {"type": "answer", "id": 123,
           "url": "https://www.zhihu.com/question/1/answer/2",
           "title": "标题", "author": "作者", "is_deleted": False}
    item = normalize_item(raw)
    assert item["key"] == "answer:123"
    assert item["title"] == "标题" and item["author"] == "作者"
    assert item["url"].endswith("/answer/2")
    assert item["is_deleted"] is False


def test_normalize_item_missing_fields():
    item = normalize_item({})
    assert item["key"] == "unknown:"
    assert item["title"] == "" and item["author"] == ""
    assert item["is_deleted"] is False


def test_diff_no_change():
    items = [_item("answer:1"), _item("answer:2", "t2")]
    diff = diff_items(items, list(items))
    assert diff["removed"] == [] and diff["added"] == [] and diff["newly_deleted"] == []


def test_diff_removed_added_deleted():
    prev = [_item("answer:1"), _item("answer:2", "消失的"), _item("article:9", "将被删")]
    cur = [_item("answer:1"), _item("article:9", "将被删", deleted=True), _item("answer:3", "新的")]
    diff = diff_items(prev, cur)
    assert [x["key"] for x in diff["removed"]] == ["answer:2"]
    assert [x["key"] for x in diff["added"]] == ["answer:3"]
    assert [x["key"] for x in diff["newly_deleted"]] == ["article:9"]


def test_report_silent_when_only_additions():
    prev = [_item("answer:1")]
    cur = [_item("answer:1"), _item("answer:2")]
    diff = diff_items(prev, cur)
    assert build_change_report(diff, 1, 2, None) == ""


def test_report_mentions_removed_item():
    prev = [_item("answer:2", "消失的标题", "作者甲", "https://www.zhihu.com/q/1/a/2")]
    diff = diff_items(prev, [])
    report = build_change_report(diff, 1, 0, None)
    assert "消失" in report
    assert "消失的标题" in report and "作者甲" in report
    assert "https://www.zhihu.com/q/1/a/2" in report
    assert "回答" in report  # 类型中文化


def test_report_marks_newly_deleted():
    prev = [_item("answer:7", "被删的")]
    cur = [_item("answer:7", "被删的", deleted=True)]
    diff = diff_items(prev, cur)
    report = build_change_report(diff, 1, 1, None)
    assert "已删除" in report and "被删的" in report


def test_report_no_liveness_for_articles():
    # 文章不做存活判定（接口实测 403），报告中不应出现“可访问”结论
    prev = [_item("article:5", "文章", "作者", "https://zhuanlan.zhihu.com/p/5")]
    diff = diff_items(prev, [])
    report = build_change_report(diff, 1, 0, None)
    assert "仍可访问" not in report and "无法访问" not in report


def test_baseline_message():
    message = build_baseline_message(1079)
    assert "1079" in message and "建立基准" in message


def test_sanity_checks():
    assert scan_sanity_error(100, 100, True, None, 120) is None
    assert scan_sanity_error(100, 100, True, 100, 120) is None
    assert scan_sanity_error(None, 50, True, None, 120) is None
    assert scan_sanity_error(1000, 950, True, 1200, 120) is not None  # 相对上次骤降>10%
    assert scan_sanity_error(100, 100, False, None, 120) is not None  # 未到末尾
    assert scan_sanity_error(100, 60, True, None, 120) is not None    # 少于 totals 70%
    assert scan_sanity_error(100, 80, True, None, 120) is None        # totals 差距属正常范围
    assert scan_sanity_error(1000, 999, True, 1000, 120) is None      # 轻微波动不算


def test_type_cn():
    assert type_cn("answer") == "回答"
    assert type_cn("article") == "文章"
    assert type_cn("mystery") == "mystery"


def test_snapshot_roundtrip_and_corrupt_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        payload = {"collection": "x", "count": 2,
                   "items": [_item("answer:1"), _item("answer:2")]}
        save_snapshot(state, payload)
        loaded, note = load_previous(state)
        assert note == "" and loaded is not None and len(loaded["items"]) == 2

        (state / "latest.json").write_text("{broken", encoding="utf-8")
        loaded, note = load_previous(state)
        assert loaded is None and "损坏" in note
        assert any(p.name.startswith("latest.corrupt-") for p in state.iterdir())


def test_history_prune_keeps_recent():
    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history"
        history.mkdir()
        for i in range(HISTORY_KEEP + 5):
            (history / f"{i:05d}.json").write_text("{}", encoding="utf-8")
        prune_history(history, keep=HISTORY_KEEP)
        remaining = sorted(p.name for p in history.glob("*.json"))
        assert len(remaining) == HISTORY_KEEP
        assert "00000.json" not in remaining  # 最旧的被清掉
        assert f"{HISTORY_KEEP + 4:05d}.json" in remaining  # 最新的保留


if __name__ == "__main__":
    test_functions = [value for name, value in sorted(globals().items())
                      if name.startswith("test_") and callable(value)]
    for function in test_functions:
        function()
        print(f"ok - {function.__name__}")
    print(f"all {len(test_functions)} tests passed")

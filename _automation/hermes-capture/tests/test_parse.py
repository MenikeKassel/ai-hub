import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture_pipeline as pipeline

from capture_pipeline import (
    classify_project,
    classify_source_type,
    has_usable_x_payload_content,
    is_zhihu_local_browser_url,
    extract_x_profile_handle,
    looks_like_blocked_page,
    manual_supplement_result,
    needs_manual_supplement,
    parse_message,
    should_write_obsidian,
)
from hermes_plugin import gateway_capture_rewrite_text


def test_parse_clip():
    parsed = parse_message("/clip https://example.com hello")
    assert parsed["command"] == "clip"
    assert parsed["url"] == "https://example.com"
    assert parsed["note"] == "hello"


def test_parse_idea():
    parsed = parse_message("/idea build KOL index")
    assert parsed["command"] == "idea"
    assert parsed["url"] is None
    assert parsed["note"] == "build KOL index"


def test_parse_log():
    parsed = parse_message("/log 今天整理了防亏系统，晚上复盘")
    assert parsed["command"] == "log"
    assert parsed["url"] is None
    assert parsed["note"] == "今天整理了防亏系统，晚上复盘"


def test_parse_trading_object_commands():
    kol = parse_message("/kol https://x.com/public_kol_5 交易心理")
    assert kol["command"] == "kol"
    assert kol["url"] == "https://x.com/public_kol_5"
    assert kol["note"] == "交易心理"

    event = parse_message("/event https://x.com/a/status/1 推荐高德红外")
    assert event["command"] == "event"
    assert event["url"] == "https://x.com/a/status/1"
    assert event["note"] == "推荐高德红外"

    concept = parse_message("/concept 大周期高位放量")
    assert concept["command"] == "concept"
    assert concept["url"] is None
    assert concept["note"] == "大周期高位放量"

    holding = parse_message("/holding 我买了159139，建仓价格1.460")
    assert holding["command"] == "holding"
    assert holding["url"] is None
    assert holding["note"] == "我买了159139，建仓价格1.460"


def test_parse_day_alias():
    parsed = parse_message("/day 今天读了财报")
    assert parsed["command"] == "log"
    assert parsed["note"] == "今天读了财报"


def test_parse_auto_raw_url_share():
    parsed = parse_message("3.51 copy share https://v.douyin.com/LmvzVVWgiiU/ tail")
    assert parsed["command"] == "clip"
    assert parsed["url"] == "https://v.douyin.com/LmvzVVWgiiU/"
    assert parsed["note"] == "3.51 copy share tail"


def test_parse_auto_command_plain_text_log():
    parsed = parse_message("/auto today handled hermes capture")
    assert parsed["command"] == "log"
    assert parsed["url"] is None
    assert parsed["note"] == "today handled hermes capture"


def test_gateway_rewrites_raw_link_only():
    assert (
        gateway_capture_rewrite_text("share https://example.com/a", "feishu")
        == "/auto share https://example.com/a"
    )
    assert gateway_capture_rewrite_text("/clip https://example.com/a", "feishu") is None
    assert gateway_capture_rewrite_text("today handled hermes capture", "feishu") is None
    assert (
        gateway_capture_rewrite_text("today handled hermes capture", "feishu", auto_text_logs=True)
        == "/auto today handled hermes capture"
    )


def test_classify_source_type():
    assert classify_source_type("https://x.com/user/status/1") == "X"
    assert classify_source_type("https://www.zhihu.com/question/1") == "知乎"
    assert classify_source_type(None) == "自己想法"


def test_extract_x_profile_handle():
    assert extract_x_profile_handle("https://x.com/Public KOL 6") == "Public KOL 6"
    assert extract_x_profile_handle("https://twitter.com/public_kol_5") == "public_kol_5"
    assert extract_x_profile_handle("https://x.com/a/status/1") == ""
    assert extract_x_profile_handle("https://x.com/i/status/1") == ""


def test_zhihu_local_browser_url_includes_answer_pages():
    assert is_zhihu_local_browser_url("https://www.zhihu.com/question/1")
    assert is_zhihu_local_browser_url("https://www.zhihu.com/question/1/answer/2?share_code=x")
    assert is_zhihu_local_browser_url("https://www.zhihu.com/pin/2057415674063069827?native=1")
    assert not is_zhihu_local_browser_url("https://zhuanlan.zhihu.com/p/123")


def test_classify_project_names():
    assert classify_project("KOL指数思路", "自己想法") == "KOL指数"
    assert classify_project("宝妈指数思路", "自己想法") == "宝妈指数"
    assert classify_project("A股选股回测", "网页") == "交易系统"
    assert classify_project("如何看待2026年7月7日的A股行情？ 明天关注股票", "知乎") == "交易系统"
    assert classify_project("A股消费板块和半导体行情", "知乎") == "交易系统"


def test_obsidian_only_accepts_trading_projects():
    config = {"obsidian_project_allowlist": "交易系统;KOL指数;基本面量化系统"}
    assert should_write_obsidian({"command": "clip", "project": "交易系统"}, config)
    assert should_write_obsidian({"command": "kol", "project": "KOL指数"}, config)
    assert not should_write_obsidian({"command": "clip", "project": "知识管理"}, config)
    assert not should_write_obsidian({"command": "idea", "project": "生活记录"}, config)
    assert not should_write_obsidian({"command": "log", "project": "交易系统"}, config)


def test_build_item_command_object_hints_without_fetch():
    kol = pipeline.build_item(
        {"command": "kol", "url": "https://x.com/Public KOL 6", "note": "待选"},
        {"max_content_chars": "8000"},
    )
    assert kol["fetch_ok"] is True
    assert kol["fetch_method"] == "x-profile-kol-fallback"
    assert kol["project"] == "KOL指数"
    assert kol["object_hint"] == "KOL实体线索"
    assert "@Public KOL 6" in kol["content"]

    concept = pipeline.build_item(
        {"command": "concept", "url": None, "note": "大周期高位放量"},
        {"max_content_chars": "8000"},
    )
    assert concept["project"] == "交易系统"
    assert concept["info_type"] == "概念线索"
    assert concept["object_hint"] == "概念线索"

    holding = pipeline.build_item(
        {"command": "holding", "url": None, "note": "我买了159139，建仓价格1.460"},
        {"max_content_chars": "8000"},
    )
    assert holding["project"] == "交易系统"
    assert holding["info_type"] == "持仓审计"
    assert holding["value_score"] == 5


def test_manual_supplement_result_for_failed_clip():
    item = {
        "command": "clip",
        "url": "https://example.com/blocked",
        "fetch_ok": False,
        "fetch_error": "captcha",
        "fetch_method": "jina",
        "source_type": "web",
        "project": "knowledge",
    }
    assert needs_manual_supplement(item)
    result = manual_supplement_result(item)
    assert result["ok"] is False
    assert result["created_or_existing"] == "not_captured"
    assert result["notion_url"] == ""
    assert result["obsidian_path"] == ""
    assert result["needs_manual_supplement"] is True
    assert "captcha" in result["error"]


def test_failed_event_also_needs_manual_supplement():
    item = {
        "command": "event",
        "url": "https://x.com/a/status/1",
        "fetch_ok": False,
        "fetch_error": "reader failed",
        "fetch_method": "x",
        "source_type": "X",
        "project": "KOL指数",
    }
    assert needs_manual_supplement(item)

def test_douyin_share_text_fallback_when_reader_fails():
    original = pipeline.fetch_url_content
    try:
        pipeline.fetch_url_content = lambda url, max_chars, config=None: {
            "ok": False,
            "content": "",
            "error": "reader failed",
            "method": "douyin:fallback",
        }
        item = pipeline.build_item(
            {
                "command": "clip",
                "url": "https://v.douyin.com/xrvfV9oGyKg/",
                "note": "复制打开抖音，看看【李越保险侠的作品】百万医疗险一定要买对",
            },
            {"max_content_chars": "8000"},
        )
    finally:
        pipeline.fetch_url_content = original
    assert item["fetch_ok"] is True
    assert item["fetch_method"] == "douyin-share-text"
    assert item["source_type"] == "抖音"
    assert "百万医疗险" in item["content"]

def test_x_payload_requires_usable_content():
    assert not has_usable_x_payload_content({"tweetURL": "https://x.com/a/status/1"})
    assert has_usable_x_payload_content({"tweetURL": "https://x.com/a/status/1", "text": "hello"})
    assert has_usable_x_payload_content({"tweetURL": "https://x.com/a/status/1", "mediaURLs": ["https://img"]})
    assert has_usable_x_payload_content({"quote": {"text": "quoted"}})


def test_blocked_page_detects_chinese_verification():
    assert looks_like_blocked_page("安全验证 - 知乎")
    assert looks_like_blocked_page("请完成验证后继续访问")


if __name__ == "__main__":
    test_parse_clip()
    test_parse_idea()
    test_parse_log()
    test_parse_trading_object_commands()
    test_parse_day_alias()
    test_parse_auto_raw_url_share()
    test_parse_auto_command_plain_text_log()
    test_gateway_rewrites_raw_link_only()
    test_classify_source_type()
    test_extract_x_profile_handle()
    test_zhihu_local_browser_url_includes_answer_pages()
    test_classify_project_names()
    test_obsidian_only_accepts_trading_projects()
    test_build_item_command_object_hints_without_fetch()
    test_manual_supplement_result_for_failed_clip()
    test_failed_event_also_needs_manual_supplement()
    test_douyin_share_text_fallback_when_reader_fails()
    test_x_payload_requires_usable_content()
    test_blocked_page_detects_chinese_verification()
    print("ok")

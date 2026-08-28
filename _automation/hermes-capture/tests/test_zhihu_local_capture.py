import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhihu_local_capture import (
    answer_id_from_url,
    capture_zhihu,
    format_markdown,
    is_supported_zhihu_url,
    pin_id_from_url,
    question_id_from_url,
)


def test_zhihu_url_parsing():
    question = "https://www.zhihu.com/question/2055737047265161338"
    answer = "https://www.zhihu.com/question/2055737047265161338/answer/788270550"
    pin = "https://www.zhihu.com/pin/2057415674063069827?native=1"
    assert is_supported_zhihu_url(question)
    assert is_supported_zhihu_url(answer)
    assert is_supported_zhihu_url(pin)
    assert question_id_from_url(question) == "2055737047265161338"
    assert question_id_from_url(answer) == "2055737047265161338"
    assert answer_id_from_url(answer) == "788270550"
    assert pin_id_from_url(pin) == "2057415674063069827"
    assert not is_supported_zhihu_url("https://example.com/question/1")


def test_format_markdown_contains_answers():
    content = format_markdown(
        "Question title",
        "https://www.zhihu.com/question/1",
        [
            {
                "author": "Alice",
                "url": "https://www.zhihu.com/question/1/answer/2",
                "voteup_count": 3,
                "comment_count": 4,
                "text": "First line\n\nSecond line",
            }
        ],
        "zhihu-local-browser:dom",
    )
    assert "# Question title" in content
    assert "### 1. Alice" in content
    assert "Voteup: 3" in content
    assert "First line" in content


def test_capture_without_cdp_fails_cleanly():
    result = capture_zhihu(
        url="https://www.zhihu.com/question/2055737047265161338",
        port=9,
        chrome_path="chrome-does-not-matter.exe",
        profile_directory="Profile 2",
        user_data_dir="",
        launch=False,
        wait_seconds=0,
        max_answers=1,
        max_scrolls=0,
        max_chars=500,
    )
    assert result["ok"] is False
    assert result["method"] == "zhihu-local-browser"
    assert "DevTools" in result["error"]


if __name__ == "__main__":
    test_zhihu_url_parsing()
    test_format_markdown_contains_answers()
    test_capture_without_cdp_fails_cleanly()
    print("ok")
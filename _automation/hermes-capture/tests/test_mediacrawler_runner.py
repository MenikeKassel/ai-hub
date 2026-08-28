import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture_pipeline import maybe_enqueue_mediacrawler
from mediacrawler_runner import build_job, detect_platform, is_zhihu_question_page


def test_detect_platform():
    assert detect_platform("https://www.zhihu.com/question/1") == "zhihu"
    assert detect_platform("https://www.bilibili.com/video/BV1xx411c7mD") == "bili"
    assert detect_platform("https://v.douyin.com/abc/") == "dy"
    assert detect_platform("http://xhslink.com/o/abc") == "xhs"
    assert is_zhihu_question_page("https://www.zhihu.com/question/1") is True
    assert is_zhihu_question_page("https://www.zhihu.com/question/1/answer/2") is False
    assert is_zhihu_question_page("https://zhuanlan.zhihu.com/p/123") is False


def test_build_detail_job_with_fake_repo():
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        (repo / "main.py").write_text("print('fake')\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(repo),
            platform="auto",
            type="detail",
            url=["https://www.zhihu.com/question/1/answer/2"],
            id=[],
            creator_id=[],
            keyword=[],
            keywords="",
            login="qrcode",
            cookies="",
            save_option="jsonl",
            save_path=str(repo / "out"),
            comments="true",
            sub_comments="false",
            max_comments=5,
            max_notes=3,
            concurrency=1,
            headless="false",
        )
        job = build_job(args)
        assert job["platform"] == "zhihu"
        assert "--specified_id" in job["command"]
        assert "https://www.zhihu.com/question/1/answer/2" in job["command"]


def test_build_zhihu_question_detail_is_rejected():
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        (repo / "main.py").write_text("print('fake')\n", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(repo),
            platform="auto",
            type="detail",
            url=["https://www.zhihu.com/question/1"],
            id=[],
            creator_id=[],
            keyword=[],
            keywords="",
            login="qrcode",
            cookies="",
            save_option="jsonl",
            save_path=str(repo / "out"),
            comments="true",
            sub_comments="false",
            max_comments=5,
            max_notes=3,
            concurrency=1,
            headless="false",
        )
        try:
            build_job(args)
        except ValueError as exc:
            assert "does not support bare /question/" in str(exc)
        else:
            raise AssertionError("expected bare Zhihu question URL to be rejected")


def test_capture_failure_enqueue_is_idempotent():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        repo = root / "MediaCrawler"
        repo.mkdir()
        (repo / "main.py").write_text("print('fake')\n", encoding="utf-8")
        queue_file = root / "queue.jsonl"
        item = {
            "command": "clip",
            "url": "https://www.zhihu.com/question/1/answer/2",
            "source_type": "知乎",
            "fetch_ok": False,
            "summary": "读取失败",
        }
        config = {
            "enable_mediacrawler_queue": "true",
            "mediacrawler_repo": str(repo),
            "mediacrawler_queue_file": str(queue_file),
            "mediacrawler_max_comments": "5",
            "mediacrawler_max_notes": "3",
        }
        first = maybe_enqueue_mediacrawler(dict(item), config)
        second = maybe_enqueue_mediacrawler(dict(item), config)
        assert first["heavy_backend_status"] == "queued"
        assert second["heavy_backend_status"] == "existing"
        assert queue_file.read_text(encoding="utf-8").count("\n") == 1


def test_capture_zhihu_question_needs_specific_url():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        repo = root / "MediaCrawler"
        repo.mkdir()
        (repo / "main.py").write_text("print('fake')\n", encoding="utf-8")
        queue_file = root / "queue.jsonl"
        item = {
            "command": "clip",
            "url": "https://www.zhihu.com/question/1",
            "source_type": "知乎",
            "fetch_ok": False,
            "summary": "读取失败",
        }
        config = {
            "enable_mediacrawler_queue": "true",
            "mediacrawler_repo": str(repo),
            "mediacrawler_queue_file": str(queue_file),
            "mediacrawler_max_comments": "5",
            "mediacrawler_max_notes": "3",
        }
        result = maybe_enqueue_mediacrawler(dict(item), config)
        assert result["heavy_backend_status"] == "needs_specific_url"
        assert not queue_file.exists()


if __name__ == "__main__":
    test_detect_platform()
    test_build_detail_job_with_fake_repo()
    test_build_zhihu_question_detail_is_rejected()
    test_capture_failure_enqueue_is_idempotent()
    test_capture_zhihu_question_needs_specific_url()
    print("ok")

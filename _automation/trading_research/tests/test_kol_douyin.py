from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_discovery_runtime import LocalCaptureAccountProvider  # noqa: E402
from kol_posts import KolPostStore, normalise_douyin_post  # noqa: E402
from kol_sources.douyin_capture import (  # noqa: E402
    build_captures,
    index_transcripts,
    item_text,
    sync_douyin_captures,
)
from kol_sources.providers import CaptureAccountPostProvider  # noqa: E402

SEC_UID = "MS4wLjABAAAAK713M9d8PGNb_WiMYf7yKhOI5y60H4uELJK2guDjJT0"
AWEME_A = "7631481216667922417"
AWEME_B = "7655600433772651889"


def manifest_row(aweme_id: str, *, author: str = "模型先生", date: str = "2026-07-10", desc: str = "no_title", media_type: str = "video") -> dict:
    return {
        "aweme_id": aweme_id,
        "author_name": author,
        "author_sec_uid": "self",
        "date": date,
        "desc": desc,
        "media_type": media_type,
        "tags": ["股市"],
        "file_paths": [f"self/collect/{aweme_id}.mp4"],
    }


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )


def write_transcript(root: Path, name: str, text: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "transcript.txt").write_text(text, encoding="utf-8")


class DouyinRegistrationTest(unittest.TestCase):
    def store(self, tmp: str) -> KolPostStore:
        return KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")

    def test_douyin_kol_registers_with_sec_uid_and_canonical_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            kol_id, created = store.add_kol(
                "模型先生", SEC_UID, platform="douyin", tracking_mode="capture"
            )
            self.assertTrue(created)
            kols = store.list_kols("active", "douyin")
            self.assertEqual(1, len(kols))
            self.assertEqual("Douyin", kols[0]["platform"])
            self.assertEqual(SEC_UID, kols[0]["handle"])
            self.assertEqual(f"https://www.douyin.com/user/{SEC_UID}", kols[0]["profile_url"])
            self.assertEqual(kol_id, kols[0]["id"])

    def test_douyin_kol_rejects_malformed_sec_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            with self.assertRaises(ValueError):
                store.add_kol("模型先生", "not a sec uid!", platform="douyin")

    def test_unknown_platform_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            with self.assertRaises(ValueError):
                store.add_kol("某人", "someone", platform="myspace")


class DouyinCaptureBuildTest(unittest.TestCase):
    def test_build_captures_prefers_transcript_and_keeps_description_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "download_manifest.jsonl"
            transcripts = root / "transcripts"
            write_manifest(
                manifest,
                [
                    manifest_row(AWEME_A),
                    manifest_row(AWEME_B, date="2026-08-01", desc="光模块龙头观察"),
                    manifest_row("999999", author="别人", date="2026-08-01", desc="忽略"),
                ],
            )
            write_transcript(
                transcripts,
                f"2026-07-10_no_title_{AWEME_A}",
                "7月初科技股刚开始调整的时候我们说要重点观察四个龙头",
            )
            kols = [
                {
                    "id": 1,
                    "platform": "Douyin",
                    "handle": SEC_UID,
                    "display_name": "模型先生",
                    "profile_url": "",
                }
            ]
            payload = build_captures(kols, manifest=manifest, transcripts=transcripts)
            self.assertEqual(1, len(payload["accounts"]))
            self.assertEqual(SEC_UID, payload["accounts"][0]["external_account_id"])
            self.assertEqual(2, len(payload["content"]))
            self.assertEqual(2, payload["stats"]["items"])
            self.assertEqual(1, payload["stats"]["with_transcript"])
            self.assertEqual(1, payload["stats"]["with_description"])
            by_id = {item["external_item_id"]: item for item in payload["content"]}
            self.assertIn("四个龙头", by_id[AWEME_A]["text"])
            self.assertEqual("transcript", by_id[AWEME_A]["text_source"])
            self.assertEqual("description", by_id[AWEME_B]["text_source"])
            self.assertEqual(f"https://www.douyin.com/video/{AWEME_A}", by_id[AWEME_A]["url"])
            self.assertEqual(SEC_UID, by_id[AWEME_A]["external_account_id"])

    def test_build_captures_skips_items_without_usable_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "download_manifest.jsonl"
            transcripts = root / "transcripts"
            write_manifest(manifest, [manifest_row(AWEME_A, desc="no_title")])
            transcripts.mkdir()
            payload = build_captures(
                [
                    {
                        "id": 1,
                        "platform": "douyin",
                        "handle": SEC_UID,
                        "display_name": "模型先生",
                    }
                ],
                manifest=manifest,
                transcripts=transcripts,
            )
            self.assertEqual([], payload["content"])
            self.assertEqual(1, payload["stats"]["skipped_no_text"])

    def test_item_text_falls_back_to_description(self) -> None:
        row = {"desc": "存量博弈，注意节奏"}
        text, source = item_text(AWEME_A, row, {})
        self.assertEqual("存量博弈，注意节奏", text)
        self.assertEqual("description", source)

    def test_index_transcripts_matches_aweme_id_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_transcript(root, f"2026-07-10_no_title_{AWEME_A}", "示例")
            index = index_transcripts(root)
            self.assertEqual(root / f"2026-07-10_no_title_{AWEME_A}", index[AWEME_A])


class DouyinCaptureSyncTest(unittest.TestCase):
    def test_sync_writes_capture_files_and_provider_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = store.add_kol("模型先生", SEC_UID, platform="douyin")
            manifest = root / "download_manifest.jsonl"
            transcripts = root / "transcripts"
            write_manifest(manifest, [manifest_row(AWEME_A)])
            write_transcript(
                transcripts,
                f"2026-07-10_no_title_{AWEME_A}",
                "7月初科技股刚开始调整的时候我们说要重点观察四个龙头",
            )
            capture_root = root / "captures"
            result = sync_douyin_captures(
                store,
                manifest=manifest,
                transcripts=transcripts,
                capture_root=capture_root,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["kols"])
            self.assertEqual(1, result["items"])
            accounts_path = capture_root / "douyin" / "accounts.json"
            content_path = capture_root / "douyin" / "content.json"
            self.assertTrue(accounts_path.is_file())
            self.assertTrue(content_path.is_file())

            discovery = LocalCaptureAccountProvider("douyin", capture_root)
            account = discovery.resolve(SEC_UID)
            self.assertEqual(SEC_UID, account.external_account_id)
            self.assertEqual("模型先生", account.display_name)

            # The ingest loop calls fetch_user_posts; exercise the adapter.
            adapter = CaptureAccountPostProvider(discovery)
            self.assertEqual("ai-hub-local-capture-v1", adapter.name)
            fetch_result = adapter.fetch_user_posts(SEC_UID, 50)
            self.assertEqual(1, len(fetch_result.posts))
            payload = fetch_result.posts[0]
            self.assertEqual(f"douyin:{AWEME_A}", payload["post_id"])

            kol = {"id": kol_id, "handle": SEC_UID, "display_name": "模型先生"}
            post = normalise_douyin_post(payload, kol, provider=fetch_result.provider)
            self.assertEqual(f"douyin:{AWEME_A}", post.post_id)
            self.assertEqual("douyin", post.platform)
            self.assertEqual("video", post.post_type)
            self.assertIn("四个龙头", post.text)
            self.assertEqual(f"https://www.douyin.com/video/{AWEME_A}", post.url)
            self.assertTrue(post.posted_at_utc)
            self.assertEqual(kol_id, post.kol_id)

    def test_sync_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            store.add_kol("模型先生", SEC_UID, platform="douyin")
            manifest = root / "download_manifest.jsonl"
            write_manifest(manifest, [manifest_row(AWEME_A)])
            transcripts = root / "transcripts"
            write_transcript(transcripts, f"2026-07-10_no_title_{AWEME_A}", "示例文本")
            capture_root = root / "captures"
            result = sync_douyin_captures(
                store,
                manifest=manifest,
                transcripts=transcripts,
                capture_root=capture_root,
                dry_run=True,
            )
            self.assertNotIn("written", result)
            self.assertFalse((capture_root / "douyin" / "accounts.json").exists())


class DouyinNormalisationTest(unittest.TestCase):
    def test_normalise_rejects_missing_text(self) -> None:
        with self.assertRaises(ValueError):
            normalise_douyin_post({"external_item_id": AWEME_A, "text": " "}, {"id": 1, "handle": SEC_UID})

    def test_normalise_rejects_bad_item_id(self) -> None:
        with self.assertRaises(ValueError):
            normalise_douyin_post({"external_item_id": "abc", "text": "示例"}, {"id": 1, "handle": SEC_UID})

    def test_gallery_items_use_note_url(self) -> None:
        post = normalise_douyin_post(
            {
                "external_item_id": AWEME_A,
                "text": "示例",
                "media_type": "gallery",
                "posted_at": "2026-07-12T12:00:00+08:00",
            },
            {"id": 1, "handle": SEC_UID, "display_name": "模型先生"},
        )
        self.assertEqual("gallery", post.post_type)
        self.assertEqual(f"https://www.douyin.com/note/{AWEME_A}", post.url)

    def test_normalise_requires_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            normalise_douyin_post(
                {"external_item_id": AWEME_A, "text": "示例"},
                {"id": 1, "handle": SEC_UID},
            )


if __name__ == "__main__":
    unittest.main()

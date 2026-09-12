"""Offline archive integrity and HTTP fault/resume checks; no real user data."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x_likes_archive_emit as archive
import x_media_download as media


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        source.mkdir()
        self.merged, self.bird = source / "merged.jsonl", source / "bird.json"
        self.out = self.root / "output"
        self.item = {"id": "123", "created_at": "2026-09-13 00:01:00 +08:00",
                     "full_text": 'line one\n## fake heading\n![embed](https://example.com)\n<a id="bad"> & 中文',
                     "screen_name": "test_user", "name": 'Name, "comma"', "url": "https://x.com/test_user/status/123",
                     "favorite_count": 0, "retweet_count": 2, "views_count": None,
                     "media": [{"type": "animated_gif", "original": "https://example.com/a.mp4", "thumbnail": "https://example.com/a.jpg"}],
                     "unknown_field": {"preserved": [False, None, "exact"]}}
        self.prov = {"in_20260610": True, "in_20260913": False}
        self.write_main([{"id": "123", "item": self.item, "prov": self.prov}])
        self.bird.write_text(json.dumps({"tweets": [
            {"id": "123"}, {"id": "456", "created_at": "Sat Sep 12 17:00:00 +0000 2026", "full_text": "history",
                             "tweet_by_username": "old", "tweet_by_full_name": "Old Name",
                             "media": json.dumps([{"type": "photo", "url": "https://example.com/b.jpg"}])}],
            "likes": [{"tweet_id": "456"}], "bookmarks": []}), encoding="utf-8")

    def write_main(self, rows):
        self.merged.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_full_render_preserves_source_maps_history_and_is_byte_reproducible(self):
        before = [archive.sha256(p) for p in (self.merged, self.bird)]
        report = archive.emit(self.merged, self.bird, self.out, expected_main=1)
        full = json.loads((self.out / "archive/likes_full.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(full.pop("_prov"), self.prov)
        self.assertEqual(full, self.item)
        history = json.loads((self.out / "archive/likes_extras_birdbear_only.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(history["id"], "456")
        self.assertEqual(history["created_at"], "2026-09-13 01:00:00 +08:00")
        self.assertEqual(history["media"][0]["original"], "https://example.com/b.jpg")
        self.assertEqual(history["_source"], "birdbear-2025-10")
        self.assertTrue(report["ok"])
        self.assertEqual(report["monthly_sections"], 1)
        monthly = (self.out / "vault-staging/全文/2026-09.md").read_text(encoding="utf-8")
        self.assertIn(r"> \#\# fake heading", monthly)
        self.assertNotIn('<a id="bad">', monthly)
        self.assertNotIn("history", monthly)
        self.assertIn("staging_only: true", monthly)
        import csv
        with (self.out / "vault-staging/索引.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][2], self.item["name"])
        self.assertEqual(rows[1][7], "0")  # animated GIF is not counted as video
        self.assertEqual(rows[1][10], "")
        self.assertNotIn("\n", rows[1][5])
        hashes = {p.relative_to(self.out): archive.sha256(p) for p in self.out.rglob("*") if p.is_file()}
        archive.emit(self.merged, self.bird, self.out, expected_main=1)
        self.assertEqual(hashes, {p.relative_to(self.out): archive.sha256(p) for p in self.out.rglob("*") if p.is_file()})
        self.assertEqual(before, [archive.sha256(p) for p in (self.merged, self.bird)])

    def test_duplicate_provenance_combines_but_conflicts_fail_before_writing(self):
        rows = [{"id": "123", "item": self.item, "prov": self.prov},
                {"id": "123", "item": self.item, "prov": {"in_20260610": False, "in_20260913": True}}]
        self.write_main(rows)
        records, counts = archive.load_main(self.merged)
        self.assertEqual(counts["duplicates_removed"], 1)
        self.assertEqual(archive.provenance(records[0]), "both")
        rows[1]["item"] = dict(self.item, full_text="conflicting body")
        self.write_main(rows)
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            archive.emit(self.merged, self.bird, self.out, 1)
        self.assertFalse(self.out.exists())

    def test_count_malformed_input_and_vault_guards(self):
        with self.assertRaisesRegex(ValueError, "count"):
            archive.emit(self.merged, self.bird, self.out, 2)
        self.assertFalse(self.out.exists())
        with self.assertRaisesRegex(ValueError, "overlap"):
            archive.emit(self.merged, self.bird, self.merged.parent, 1)
        self.out.mkdir()
        (self.out / ".obsidian").mkdir()
        with self.assertRaisesRegex(ValueError, "vault"):
            archive.emit(self.merged, self.bird, self.out, 1)
        self.merged.write_text("{broken\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "line 1"):
            archive.load_main(self.merged)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_url_list_query_names_and_conflicts(self):
        listing = self.root / "urls.txt"
        listing.write_text("# sample\nhttps://example.com/image?format=png&name=orig\n  out=测试 图.png\nhttps://example.com/v.mp4 out=clip.mp4\nhttps://example.com/plain?format=jpg\n", encoding="utf-8")
        assets = media.parse_url_list(listing)
        self.assertEqual(len(assets), 3)
        self.assertEqual(assets[0].filename, "测试 图.png")
        self.assertTrue(assets[2].filename.endswith(".jpg"))
        self.assertNotEqual(media.auto_name("https://example.com/i?name=a"), media.auto_name("https://example.com/i?name=b"))
        for name in ("../outside", "D:\\outside", "a/b", "CON.png", "nul", "bad.", "manifest.jsonl"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                media.check_name(name)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            media.deduplicate([media.Asset("https://example.com/a", "same.png"), media.Asset("https://example.com/b", "SAME.png")])
        with self.assertRaises(ValueError):
            media.check_url("https://user:password@example.com/a")

    def test_jsonl_all_originals_nested_media_and_optional_thumbnails(self):
        path = self.root / "likes.jsonl"
        path.write_text(json.dumps({"id": "1", "media": [
            {"type": "video", "original": "https://example.com/a.mp4", "thumbnail": "https://example.com/t.jpg"},
            {"type": "video", "original": "https://example.com/a.mp4"}],
            "quoted_status": {"id": "2", "media": [{"type": "photo", "original": "https://example.com/b.jpg"}]}}) + "\n", encoding="utf-8")
        self.assertEqual(len(media.extract_jsonl(path)), 2)
        self.assertEqual(len(media.extract_jsonl(path, True)), 3)
        report = media.run(media.extract_jsonl(path), self.root / "plan")
        self.assertEqual(report["network_requests"], 0)
        self.assertFalse((self.root / "plan/manifest.jsonl").exists())

    def test_http_retries_byte_resume_range_ignored_failures_and_verified_skip(self):
        payload = bytes(range(256)) * 1200
        calls = []
        counts = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                counts[self.path] = counts.get(self.path, 0) + 1
                calls.append((self.path, self.headers.get("Range"), self.headers.get("If-Range")))
                if self.path == "/missing":
                    self.send_error(404)
                    return
                if self.path == "/retry" and counts[self.path] == 1:
                    self.send_error(503)
                    return
                start = int(self.headers["Range"].split("=")[1].split("-")[0]) if self.headers.get("Range") and self.path != "/ignore" else 0
                self.send_response(206 if start else 200)
                self.send_header("Content-Type", "image/png")
                self.send_header("ETag", '"fixture-v1"')
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.send_header("Content-Length", str(len(payload) - start))
                self.end_headers()
                if self.path in ("/resume", "/ignore") and counts[self.path] == 1:
                    self.wfile.write(payload[:100000])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.wfile.write(payload[start:])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        assets = [media.Asset(base + path, path[1:] + ".png") for path in ("/resume", "/ignore", "/retry", "/missing")]
        out = self.root / "download"
        with contextlib.redirect_stdout(io.StringIO()):
            first = media.run(assets, out, True, workers=3, retries=2, retry_delay=0)
        self.assertEqual((first["downloaded"], first["failed"]), (3, 1))
        for name in ("resume", "ignore", "retry"):
            self.assertEqual((out / (name + ".png")).read_bytes(), payload)
        self.assertTrue(any(path == "/resume" and span and validator == '"fixture-v1"' for path, span, validator in calls))
        self.assertEqual(counts["/missing"], 1)
        self.assertEqual([a.url for a in media.parse_url_list(out / "failures.txt")], [base + "/missing"])
        count_before = len(calls)
        with contextlib.redirect_stdout(io.StringIO()):
            second = media.run(assets[:3], out, True, retry_delay=0)
        self.assertEqual(second["skipped"], 3)
        self.assertEqual(len(calls), count_before)
        self.assertEqual((out / "failures.txt").read_text(), "")
        (out / "resume.png").write_bytes(b"user-modified")
        with contextlib.redirect_stdout(io.StringIO()):
            third = media.run(assets[:1], out, True, retry_delay=0)
        self.assertEqual(third["failed"], 1)
        self.assertEqual((out / "resume.png").read_bytes(), b"user-modified")


if __name__ == "__main__":
    unittest.main()

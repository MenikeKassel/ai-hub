import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

spec = importlib.util.spec_from_file_location("import_sources", Path(__file__).with_name("import_sources.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class SourceImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.db = self.root / "posts.db"
        fields = [*bridge.FIELDS, "fetched_at", "content_hash"]
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("CREATE TABLE posts (" + ",".join(f"{f} TEXT" for f in fields) + ")")
            data = {f: "" for f in fields}
            data.update(post_id="12345", platform="X", text="厄尔尼诺测试原文", author_name="测试作者",
                        url="https://example.test/post/12345", posted_at="2026-09-01T00:00:00+08:00",
                        fetched_at="2026-09-12T00:00:00+08:00", media_json="[]", local_media_json="[]")
            c.execute("INSERT INTO posts VALUES (" + ",".join("?" for _ in fields) + ")", list(data.values()))
        self.manifest = self.root / "pilot.json"
        self.manifest.write_text(json.dumps({"vault": str(self.vault), "source_db": str(self.db),
                                            "sources": [{"platform": "X", "post_id": "12345"}]}), encoding="utf-8")

    def test_repeat_and_fetch_metadata_do_not_duplicate_or_mutate_source(self):
        db_before = self.db.read_bytes()
        first = bridge.import_sources(self.manifest)
        self.assertEqual(db_before, self.db.read_bytes())
        raw = self.vault / first["sources"][0]["raw_path"]
        content = raw.read_bytes()
        self.assertEqual(bridge.import_sources(self.manifest)["new_versions"], 0)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE posts SET fetched_at='2026-09-13T00:00:00+08:00'")
        self.assertEqual(bridge.import_sources(self.manifest)["new_versions"], 0)
        self.assertEqual(raw.read_bytes(), content)

    def test_changed_text_creates_version_preserving_old_file(self):
        first = bridge.import_sources(self.manifest)
        original = self.vault / first["sources"][0]["raw_path"]
        content = original.read_bytes()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE posts SET text='作者修订了原文'")
        second = bridge.import_sources(self.manifest)
        self.assertEqual(second["new_versions"], 1)
        self.assertNotEqual(first["sources"][0]["raw_path"], second["sources"][0]["raw_path"])
        self.assertEqual(original.read_bytes(), content)

    def test_tampered_snapshot_is_rejected_without_overwrite(self):
        first = bridge.import_sources(self.manifest)
        raw = self.vault / first["sources"][0]["raw_path"]
        raw.write_text("人工修改", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or modified"):
            bridge.import_sources(self.manifest)
        self.assertEqual(raw.read_text(encoding="utf-8"), "人工修改")

    def test_wrong_platform_does_not_import_another_identity(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["sources"][0]["platform"] = "Zhihu"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source not found"):
            bridge.import_sources(self.manifest)
        self.assertFalse((self.vault / bridge.RAW_ROOT).exists())

    def test_relocated_manifest_redirect_and_registry(self):
        canonical = self.root / "canonical.json"
        canonical.write_bytes(self.manifest.read_bytes())
        self.manifest.write_text(json.dumps({"redirect": str(canonical)}), encoding="utf-8")
        first = bridge.import_sources(self.manifest)
        self.assertEqual(Path(first["state"]), canonical.with_suffix(".state.json"))
        wiki = self.vault / bridge.WIKI_ROOT
        registry = json.loads((wiki / ".llm-wiki/archive-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry["files"]), 1)
        self.assertTrue(all((wiki / p).is_file() for p in registry["files"]))
        self.assertEqual(bridge.import_sources(canonical)["new_versions"], 0)

    def test_wiki_path_escape_is_rejected(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["wiki_root"] = "../outside"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "subdirectory"):
            bridge.import_sources(self.manifest)

    def test_same_source_in_another_batch_reuses_original_capture(self):
        first = bridge.import_sources(self.manifest)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE posts SET fetched_at='2026-09-14T00:00:00+08:00'")
        another = self.root / "another-batch.json"
        another.write_bytes(self.manifest.read_bytes())
        second = bridge.import_sources(another)
        self.assertEqual(second["new_versions"], 0)
        self.assertEqual(second["sources"][0]["raw_path"], first["sources"][0]["raw_path"])
        self.assertEqual(second["sources"][0]["source_fetched_at"], first["sources"][0]["source_fetched_at"])


if __name__ == "__main__":
    unittest.main()

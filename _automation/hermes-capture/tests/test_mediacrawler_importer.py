import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediacrawler_importer import collect_bundles, import_command


def test_collect_bundles_and_import_obsidian():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        output_root = root / "mediacrawler"
        xhs_jsonl = output_root / "xhs" / "jsonl"
        xhs_jsonl.mkdir(parents=True)
        content = {
            "note_id": "note123",
            "title": "宝妈都在买的早餐机",
            "desc": "销量和评论都很高，适合做宝妈指数样本。",
            "note_url": "https://www.xiaohongshu.com/explore/note123?xsec_token=t",
            "nickname": "masked_user",
            "liked_count": "99",
            "comment_count": "2",
        }
        comment = {
            "comment_id": "c1",
            "note_id": "note123",
            "content": "我家也买了这个。",
            "nickname": "mom_a",
            "like_count": 3,
        }
        write_jsonl(xhs_jsonl / "detail_contents_2026-07-02.jsonl", [content])
        write_jsonl(xhs_jsonl / "detail_comments_2026-07-02.jsonl", [comment])

        bundles = collect_bundles(output_root, platform_hint="xhs")
        assert len(bundles) == 1
        assert bundles[0].content_id == "note123"
        assert bundles[0].comments[0]["content"] == "我家也买了这个。"

        vault = root / "vault"
        config_file = root / "config.yaml"
        vault_text = str(vault).replace("\\", "\\\\")
        config_file.write_text(f'vault_path: "{vault_text}"\n', encoding="utf-8")

        imported = import_command(
            output_root=output_root,
            queue_file=root / "queue.jsonl",
            config_path=config_file,
            platform="xhs",
            job_id="",
            write_notion=False,
            limit=0,
        )
        assert imported["ok"] is True
        assert imported["count"] == 1
        rel_path = imported["imported"][0]["obsidian_path"]
        note_path = vault / rel_path
        text = note_path.read_text(encoding="utf-8")
        assert "宝妈都在买的早餐机" in text
        assert "我家也买了这个。" in text

        imported_again = import_command(
            output_root=output_root,
            queue_file=root / "queue.jsonl",
            config_path=config_file,
            platform="xhs",
            job_id="",
            write_notion=False,
            limit=0,
        )
        assert imported_again["imported"][0]["obsidian_path"] == rel_path


def write_jsonl(path: Path, rows: list[dict]):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


if __name__ == "__main__":
    test_collect_bundles_and_import_obsidian()
    print("ok")

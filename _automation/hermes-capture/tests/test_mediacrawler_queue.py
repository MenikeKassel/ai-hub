import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediacrawler_queue import list_jobs, run_id


def test_queue_list_and_run_id():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        queue_file = root / "queue.jsonl"
        repo = root / "repo"
        repo.mkdir()
        output = root / "out.txt"
        media_output = root / "mediacrawler" / "xhs" / "jsonl" / "detail_contents_2026-07-02.jsonl"
        vault = root / "vault"
        config_file = root / "config.yaml"
        vault_text = str(vault).replace("\\", "\\\\")
        config_file.write_text(f'vault_path: "{vault_text}"\n', encoding="utf-8")
        script = (
            "from pathlib import Path; import json; "
            f"Path(r'{output}').write_text('ok', encoding='utf-8'); "
            f"p=Path(r'{media_output}'); p.parent.mkdir(parents=True, exist_ok=True); "
            "row={'note_id':'note123','title':'queued import','desc':'from queue','note_url':'https://www.xiaohongshu.com/explore/note123'}; "
            "p.write_text(json.dumps(row, ensure_ascii=False)+'\\n', encoding='utf-8')"
        )
        job = {
            "job_id": "job1",
            "status": "pending",
            "platform": "xhs",
            "type": "detail",
            "repo": str(repo),
            "save_path": str(root / "mediacrawler"),
            "command": [sys.executable, "-c", script],
            "command_text": "fake command",
        }
        queue_file.write_text(json.dumps(job, ensure_ascii=False) + "\n", encoding="utf-8")

        listed = list_jobs(queue_file)
        assert listed["count"] == 1

        preview = run_id(queue_file, "job1", execute=False, timeout=10)
        assert preview["ok"] is True
        assert preview["execute"] is False

        executed = run_id(queue_file, "job1", execute=True, timeout=10, import_results=True, config_path=config_file)
        assert executed["ok"] is True
        assert output.read_text(encoding="utf-8") == "ok"
        assert executed["import_result"]["count"] == 1
        assert (vault / executed["import_result"]["imported"][0]["obsidian_path"]).exists()

        updated = list_jobs(queue_file)
        assert updated["jobs"][0]["status"] == "done"
        assert updated["jobs"][0]["imported_paths"]


if __name__ == "__main__":
    test_queue_list_and_run_id()
    print("ok")

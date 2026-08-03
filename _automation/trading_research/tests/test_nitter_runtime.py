from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nitter_runtime import docker_command, materialize_nitter_runtime, timeline_health  # noqa: E402


class FixtureCredentials:
    def session(self) -> dict[str, str]:
        return {"kind": "cookie", "auth_token": "a" * 40, "ct0": "c" * 64}


class NitterRuntimeTests(unittest.TestCase):
    def test_materialize_writes_jsonl_and_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "nitter.conf.template"
            template.write_text('hmacKey = "{HMAC_KEY}"\n', encoding="utf-8")

            result = materialize_nitter_runtime(root / "runtime", template, FixtureCredentials())

            session = json.loads(result.sessions_path.read_text(encoding="utf-8"))
            config = result.config_path.read_text(encoding="utf-8")
            self.assertEqual("cookie", session["kind"])
            self.assertEqual("a" * 40, session["auth_token"])
            self.assertNotIn("{HMAC_KEY}", config)
            self.assertNotIn("a" * 40, config)
            self.assertEqual(result.sessions_path.suffix, ".jsonl")

    def test_materialize_keeps_hmac_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "nitter.conf.template"
            template.write_text('{HMAC_KEY}', encoding="utf-8")

            first = materialize_nitter_runtime(root / "runtime", template, FixtureCredentials())
            first_config = first.config_path.read_text(encoding="utf-8")
            second = materialize_nitter_runtime(root / "runtime", template, FixtureCredentials())

            self.assertEqual(first_config, second.config_path.read_text(encoding="utf-8"))

    def test_docker_command_prefers_windows_executable(self) -> None:
        with patch("nitter_runtime.os.name", "nt"), patch(
            "nitter_runtime.shutil.which", return_value=r"C:\Program Files\Docker\docker.exe"
        ) as which:
            self.assertEqual(r"C:\Program Files\Docker\docker.exe", docker_command())
        which.assert_called_once_with("docker.exe")

    def test_timeline_health_requires_a_real_timeline_item(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self, *_):
                return b'<div class="timeline-item"><div class="tweet-content">fixture</div></div>'

        with patch("nitter_runtime.urllib.request.urlopen", return_value=Response()):
            healthy = timeline_health("http://127.0.0.1:8788", handle="fixture")
        with patch("nitter_runtime.urllib.request.urlopen", return_value=Response()) as urlopen:
            urlopen.return_value.read = lambda *_: b"<html>homepage only</html>"
            empty = timeline_health("http://127.0.0.1:8788", handle="fixture")

        self.assertTrue(healthy["ready"])
        self.assertEqual("timeline_ok", healthy["status"])
        self.assertFalse(empty["ready"])
        self.assertEqual("timeline_empty", empty["status"])


if __name__ == "__main__":
    unittest.main()

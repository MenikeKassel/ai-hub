from __future__ import annotations

import sys
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhihu_profile_capture import (  # noqa: E402
    PROFILE_FETCH_SCRIPT,
    fetch_profile,
    normalise_surfaces,
)


def test_profile_surfaces_are_configurable_and_default_to_all():
    assert normalise_surfaces(None) == ("answers", "articles", "ideas")
    assert normalise_surfaces("answer,pin,articles") == ("answers", "ideas", "articles")


def test_profile_fetch_builds_bounded_paginated_three_surface_request():
    fixture = {
        "ok": True,
        "posts": [],
        "surfaces": {
            "answers": {"status": "empty", "paging": {"is_end": True}},
            "articles": {"status": "failed", "warnings": ["http_403"]},
            "ideas": {"status": "empty", "warnings": ["empty"]},
        },
        "warnings": ["surface:articles:http_403", "surface:ideas:empty"],
        "coverage": {
            "status": "partial",
            "historical_complete": False,
            "requested_surfaces": ["answers", "articles", "ideas"],
        },
    }
    with patch("zhihu_profile_capture.local.evaluate", return_value=fixture) as evaluate:
        result = fetch_profile(object(), "target", 5, 5000)

    assert result["coverage"]["status"] == "partial"
    assert "surface:articles:http_403" in result["warnings"]
    expression = str(evaluate.call_args.args[1])
    assert "offset=" in expression
    assert "AbortController" in expression
    assert "code_10003" in PROFILE_FETCH_SCRIPT


def test_profile_fetch_js_handles_api_errors_and_pin_content_arrays():
    expression = (
        PROFILE_FETCH_SCRIPT.replace("__HANDLE__", json.dumps("target"))
        .replace("__SURFACES__", json.dumps(["answers", "articles", "ideas"]))
        .replace("__LIMIT__", "3")
        .replace("__MAX_CHARS__", "2000")
        .replace("__BUDGET_MS__", "1000")
        .replace("__REQUEST_TIMEOUT_MS__", "100")
    )
    node_source = f"""
const strip = (value) => String(value || '').replace(/<[^>]+>/g, '');
globalThis.document = {{
  createElement() {{
    const root = {{raw: '', innerText: '', textContent: ''}};
    Object.defineProperty(root, 'innerHTML', {{set(value) {{
      root.raw = String(value || '');
      root.innerText = strip(root.raw);
      root.textContent = root.innerText;
    }}}});
    root.querySelectorAll = (selector) => Array.from(
      root.raw.matchAll(/<(p|h1|h2|h3|h4|li|blockquote)[^>]*>([\\s\\S]*?)<\\/\\1>/gi)
    ).map((match) => ({{innerText: strip(match[2]), textContent: strip(match[2])}}));
    return root;
  }}
}};
globalThis.setTimeout = (callback) => {{ callback(); return 1; }};
globalThis.clearTimeout = () => {{}};
const answers = [
  {{error: {{code: 10003, message: 'rate'}}}},
  {{error: {{code: 10003, message: 'rate'}}}},
  {{error: {{code: 10003, message: 'rate'}}}}
];
globalThis.fetch = async (url) => {{
  const endpoint = String(url).split('/members/target/')[1].split('?')[0];
  if (endpoint === 'answers') {{
    return {{ok: true, status: 200, text: async () => JSON.stringify(answers.shift())}};
  }}
  if (endpoint === 'articles') {{
    return {{ok: true, status: 200, text: async () => JSON.stringify({{error: {{code: 4041}}}})}};
  }}
  return {{ok: true, status: 200, text: async () => JSON.stringify({{
    data: [{{id: '1001', content: [{{type: 'text', content: '<p>数组想法</p>'}}],
      author: {{name: '真实作者', url_token: 'target'}}, created_time: 1756000000}}],
    paging: {{is_end: true}}
  }})}};
}};
Promise.resolve({expression}).then((value) => process.stdout.write(JSON.stringify(value)));
"""
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "profile_fixture.js"
        script_path.write_text(node_source, encoding="utf-8")
        node = shutil.which("node") or "node"
        command = ["cmd.exe", "/c", node, str(script_path)] if os.name == "nt" else [node, str(script_path)]
        # The bundled runtime's node.exe is executable through cmd.exe in the
        # desktop sandbox, while direct CreateProcess can be denied by policy.
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["coverage"]["status"] == "partial"
    assert payload["surfaces"]["answers"]["warnings"][-1] == "code_10003_rate_limited"
    assert payload["surfaces"]["articles"]["warnings"] == ["code_4041"]
    assert payload["posts"][0]["text"] == "数组想法"

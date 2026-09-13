from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Protocol
import httpx
from model_budget import ModelDailyBudget, OcrDailyBudget
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL, load_opencode_go_api_key, opencode_go_headers
from .core import ModelProviderUnavailableError, _json, _raise_codex_failure
from .credentials import DeepSeekCredentialStore
from .repository import KolPostStore
from .rules import RuleClassifier, validate_model_payload


def _run_command_with_tree_timeout(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: float,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        else:
            process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise RuntimeError(f"{operation} timed out after {timeout_seconds:g} seconds") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _resolve_codex_command(command: str) -> str:
    path = Path(command)
    if path.is_absolute() or path.suffix:
        return str(path)
    if os.name != "nt":
        return shutil.which(command) or command

    candidates = [shutil.which(f"{command}.cmd")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(str(Path(appdata) / "npm" / f"{command}.cmd"))
    candidates.append(shutil.which(f"{command}.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if Path(candidate).suffix.lower() == ".exe" and "windowsapps" in candidate.lower():
            continue
        return str(Path(candidate))
    return command


def _codex_command_prefix(command: str) -> list[str]:
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() == ".cmd":
        javascript = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        bundled_node = path.parent / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node.exe") or shutil.which("node")
        if node and javascript.is_file():
            # Calling the npm .cmd shim can return before its Node child exits. Running
            # Node directly keeps the temporary JSON output alive until Codex finishes.
            return [node, str(javascript)]
    return [command]


class OcrBatchClassifier(Protocol):
    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]: ...


class RapidOcrBatchClassifier:
    provider_name = "rapidocr"

    def __init__(
        self,
        python: Path,
        runner_script: Path,
        timeout_seconds: float | None = 90,
    ):
        self.python = Path(python)
        self.runner_script = Path(runner_script)
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return self.python.is_file() and self.runner_script.is_file()

    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.available():
            raise RuntimeError(f"RapidOCR runtime is not available: {self.python}")
        manifest = [
            {
                "post_id": post["post_id"],
                "images": [
                    str(item.get("path") or "")
                    for item in post.get("local_media", [])
                    if item.get("path") and Path(str(item["path"])).is_file()
                ],
            }
            for post in posts
        ]
        with tempfile.TemporaryDirectory(prefix="kol-rapid-ocr-") as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            output_path = Path(temp_dir) / "result.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            completed = _run_command_with_tree_timeout(
                [
                    str(self.python),
                    str(self.runner_script),
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                input_text="",
                timeout_seconds=max(1, self.timeout_seconds or 90),
                operation="RapidOCR batch",
            )
            if completed.returncode != 0 or not output_path.exists():
                detail = (completed.stderr or completed.stdout or "RapidOCR failed").strip()
                raise RuntimeError(detail[-2000:])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("RapidOCR returned an invalid batch result")
        return {
            str(post_id): {
                "text": str(value.get("text") or ""),
                "error": str(value.get("error") or ""),
                "provider": self.provider_name,
                "average_confidence": float(value.get("average_confidence") or 0),
                "lines": list(value.get("lines") or []),
            }
            for post_id, value in payload.items()
            if isinstance(value, dict)
        }


class UnlimitedOcrBatchClassifier:
    def __init__(self, ocr_root: Path, runner_script: Path, timeout_seconds: float | None = None):
        self.ocr_root = Path(ocr_root)
        self.runner_script = Path(runner_script)
        self.python = self.ocr_root / ".venv" / "Scripts" / "python.exe"
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return self.python.is_file() and self.runner_script.is_file()

    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.available():
            raise RuntimeError(f"Unlimited-OCR runtime is not available: {self.ocr_root}")
        manifest = [
            {
                "post_id": post["post_id"],
                "images": [
                    str(item.get("path") or "")
                    for item in post.get("local_media", [])
                    if item.get("path") and Path(str(item["path"])).is_file()
                ],
            }
            for post in posts
        ]
        with tempfile.TemporaryDirectory(prefix="kol-ocr-") as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            output_path = Path(temp_dir) / "result.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            batch_timeout = self.timeout_seconds or max(900, len(posts) * 300)
            completed = _run_command_with_tree_timeout(
                [
                    str(self.python),
                    str(self.runner_script),
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                input_text="",
                timeout_seconds=max(1, batch_timeout),
                operation="Unlimited-OCR batch",
            )
            if completed.returncode != 0 or not output_path.exists():
                detail = (completed.stderr or completed.stdout or "Unlimited-OCR failed").strip()
                raise RuntimeError(detail[-2000:])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Unlimited-OCR returned an invalid batch result")
        return {
            str(post_id): {
                "text": str(value.get("text") or ""),
                "error": str(value.get("error") or ""),
                "provider": "unlimited-ocr",
                "average_confidence": 0,
                "lines": [],
            }
            for post_id, value in payload.items()
            if isinstance(value, dict)
        }


def process_pending_with_ocr(
    store: KolPostStore,
    classifier: OcrBatchClassifier,
    rule_classifier: RuleClassifier,
    *,
    limit: int = 8,
) -> tuple[int, int]:
    posts = store.claim_posts_for_ocr(limit)
    if not posts:
        return 0, 0
    try:
        results = classifier.classify(posts)
    except Exception as exc:
        for post in posts:
            store.save_ocr_result(
                post["post_id"],
                error=str(exc),
                provider=str(getattr(classifier, "provider_name", classifier.__class__.__name__)),
            )
        return 0, len(posts)
    completed = failed = 0
    for post in posts:
        result = results.get(post["post_id"], {})
        text = str(result.get("text") or "")
        error = str(result.get("error") or "")
        if error or not text.strip():
            store.save_ocr_result(
                post["post_id"],
                error=error or "OCR returned no text",
                provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
            )
            failed += 1
            continue
        store.save_ocr_result(
            post["post_id"],
            text,
            provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
            confidence=float(result.get("average_confidence") or 0),
            details=list(result.get("lines") or []),
        )
        store.save_rule_classification(post["post_id"], rule_classifier.classify(post, text))
        completed += 1
    return completed, failed


def process_pending_ocr_with_budget(
    store: KolPostStore,
    classifier: OcrBatchClassifier,
    rule_classifier: RuleClassifier,
    *,
    daily_limit: int = 150,
    batch_size: int = 50,
) -> tuple[int, int]:
    """Process OCR in resumable 50-item chunks under a persistent daily cap."""
    budget = OcrDailyBudget(store, daily_limit=daily_limit)
    completed = failed = 0
    chunk_limit = max(1, min(int(batch_size), 50))
    while budget.status()["remaining"] > 0 and completed + failed < max(1, int(daily_limit)):
        remaining_budget = min(
            int(budget.status()["remaining"]),
            max(1, int(daily_limit)) - completed - failed,
        )
        posts = store.claim_posts_for_ocr(min(chunk_limit, remaining_budget))
        if not posts:
            break
        reserved = []
        for post in posts:
            if budget.reserve():
                reserved.append(post)
            else:
                store.release_ocr_claim(post["post_id"])
        if not reserved:
            break
        try:
            results = classifier.classify(reserved)
        except Exception as exc:
            results = {}
            batch_error = str(exc)
        else:
            batch_error = ""
        for post in reserved:
            try:
                result = results.get(post["post_id"], {})
                text = str(result.get("text") or "")
                error = str(result.get("error") or batch_error)
                if error or not text.strip():
                    store.save_ocr_result(
                        post["post_id"],
                        error=error or "OCR returned no text",
                        provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
                    )
                    failed += 1
                    budget.finish(success=False)
                    continue
                store.save_ocr_result(
                    post["post_id"],
                    text,
                    provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
                    confidence=float(result.get("average_confidence") or 0),
                    details=list(result.get("lines") or []),
                )
                store.save_rule_classification(post["post_id"], rule_classifier.classify(post, text))
                completed += 1
                budget.finish(success=True)
            except Exception as exc:
                store.save_ocr_result(
                    post["post_id"],
                    error=str(exc),
                    provider=str(getattr(classifier, "provider_name", classifier.__class__.__name__)),
                )
                failed += 1
                budget.finish(success=False)
        if batch_error:
            break
        if len(posts) < chunk_limit:
            break
    return completed, failed


class CodexPostClassifier:
    prompt_version = "kol-post-v3"
    model_name = "codex"

    def __init__(self, schema_path: Path, workspace: Path, command: str = "codex", timeout_seconds: float = 240):
        self.schema_path = Path(schema_path)
        self.workspace = Path(workspace)
        self.command = _resolve_codex_command(command)
        self.command_prefix = _codex_command_prefix(self.command)
        self.timeout_seconds = max(1, timeout_seconds)

    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        content = {
            "post_id": post["post_id"],
            "platform": post.get("platform") or "X",
            "kol": post["display_name"],
            "handle": post["handle"],
            "posted_at": post["posted_at"],
            "post_type": post["post_type"],
            "text": post["text"],
            "article_title": post["article_title"],
            "article_text": post["article_text"],
            "quoted_text": post["quoted_text"],
            "ocr_text": post.get("ocr_text") or "",
            "rule_symbols": post.get("rule_symbols", []),
            "rule_direction": post.get("rule_direction", ""),
        }
        prompt = (
            "Classify this source item for an A-share KOL audit system. Do not give investment advice. "
            "The platform may be X or Zhihu. Zhihu answers can be longer essays, numbered lists, "
            "retrospectives, or market analysis; evaluate them by evidence, not by platform. "
            "Distinguish original pre-event recommendations from retrospective claims, secondhand "
            "content, methodology, and market commentary. Return only the requested schema. "
            "One draft must contain exactly one A-share symbol. Evidence spans must be exact, "
            "verbatim substrings from the declared source. Mark image/OCR dependency explicitly, "
            "preserve every entry condition, and distinguish recommendations from holdings, "
            "retrospectives, and analysis. When the source is Chinese, summary, thesis, and "
            "conditions must be concise Chinese; do not translate exact evidence_spans. A thesis "
            "must state the author's actual reason and must not invent a target price, holding "
            "period, or causal claim. Also classify action (buy/add/hold/watch/reduce/sell/avoid), "
            "horizon (intraday/short/swing/medium_long/unspecified), and strength "
            "(explicit/moderate/weak/unspecified); use unspecified when absent.\n\n"
            + _json(content)
        )
        with tempfile.TemporaryDirectory(prefix="kol-classify-") as temp_dir:
            output = Path(temp_dir) / "result.json"
            command = [
                *self.command_prefix,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(self.workspace),
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(output),
                "--color",
                "never",
            ]
            for item in post.get("local_media", [])[:4]:
                path = str(item.get("path") or "")
                if path and Path(path).exists():
                    command.extend(["--image", path])
            command.append("-")
            completed = _run_command_with_tree_timeout(
                command,
                input_text=prompt,
                timeout_seconds=self.timeout_seconds,
                operation="Codex classification",
            )
            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or completed.stdout or "Codex classification failed").strip()
                _raise_codex_failure(detail, "Codex classification failed")
            value = json.loads(output.read_text(encoding="utf-8"))
        return validate_model_payload(value)


class CodexBatchPostClassifier:
    prompt_version = "kol-morning-batch-v1"
    model_name = "codex-batch"

    def __init__(
        self,
        schema_path: Path,
        workspace: Path,
        command: str = "codex",
        timeout_seconds: float = 480,
    ):
        self.schema_path = Path(schema_path)
        self.workspace = Path(workspace)
        self.command = _resolve_codex_command(command)
        self.command_prefix = _codex_command_prefix(self.command)
        self.timeout_seconds = max(1, timeout_seconds)

    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not 1 <= len(posts) <= 10:
            raise ValueError("Codex batches must contain between 1 and 10 posts")
        contents = [
            {
                "post_id": post["post_id"],
                "platform": post.get("platform") or "X",
                "kol": post["display_name"],
                "handle": post["handle"],
                "posted_at": post["posted_at"],
                "post_type": post["post_type"],
                "text": post["text"],
                "article_title": post["article_title"],
                "article_text": post["article_text"],
                "quoted_text": post["quoted_text"],
                "ocr_text": post.get("ocr_text") or "",
                "rule_symbols": post.get("rule_symbols", []),
                "rule_direction": post.get("rule_direction", ""),
            }
            for post in posts
        ]
        prompt = (
            "Classify each source item for an A-share KOL evidence audit. The platform may be X or Zhihu; "
            "Zhihu answers can be longer essays, numbered stock lists, retrospectives, or market analysis. "
            "Return one result for every post_id and only the requested JSON schema. Separate original pre-event recommendations "
            "from retrospectives, holdings, analysis, secondhand content and promotion. For a "
            "multi-stock recommendation, emit one draft per stock. A stock list without individual "
            "reasons must use the concise Chinese thesis '列入当日个股分享，原帖未提供具体个股理由'. "
            "Do not use promotional text as a thesis. Evidence spans must be exact source substrings. "
            "Preserve entry conditions and mark OCR dependency. Do not give investment advice or invent "
            "symbols, reasons, prices or holding periods. For every recommendation draft also classify "
            "the author's action (buy/add/hold/watch/reduce/sell/avoid), horizon "
            "(intraday/short/swing/medium_long/unspecified), and strength "
            "(explicit/moderate/weak/unspecified). Use unspecified when the source does not say.\n\n"
            + _json({"posts": contents})
        )
        with tempfile.TemporaryDirectory(prefix="kol-batch-classify-") as temp_dir:
            output = Path(temp_dir) / "result.json"
            command = [
                *self.command_prefix,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(self.workspace),
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(output),
                "--color",
                "never",
                "-",
            ]
            completed = _run_command_with_tree_timeout(
                command,
                input_text=prompt,
                timeout_seconds=self.timeout_seconds,
                operation="Codex batch classification",
            )
            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or completed.stdout or "Codex batch classification failed").strip()
                _raise_codex_failure(detail, "Codex batch classification failed")
            raw = json.loads(output.read_text(encoding="utf-8"))
        requested = {str(post["post_id"]) for post in posts}
        results: dict[str, dict[str, Any]] = {}
        for value in raw.get("results", []):
            post_id = str(value.get("post_id") or "")
            if post_id not in requested or post_id in results:
                raise RuntimeError(f"unexpected or duplicate post_id in batch result: {post_id}")
            results[post_id] = validate_model_payload(
                {key: item for key, item in value.items() if key != "post_id"}
            )
        missing = requested - set(results)
        if missing:
            raise RuntimeError("batch result omitted posts: " + ", ".join(sorted(missing)))
        return results


def _deepseek_source_item(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_id": post["post_id"],
        "platform": post.get("platform") or "X",
        "kol": post.get("display_name") or "",
        "handle": post.get("handle") or "",
        "posted_at": post.get("posted_at") or "",
        "post_type": post.get("post_type") or "",
        "text": post.get("text") or "",
        "article_title": post.get("article_title") or "",
        "article_text": post.get("article_text") or "",
        "quoted_text": post.get("quoted_text") or "",
        "ocr_text": post.get("ocr_text") or "",
        "rule_symbols": post.get("rule_symbols", []),
        "rule_direction": post.get("rule_direction", ""),
    }


def _deepseek_review_instruction(schema: str, *, batch: bool) -> str:
    target = "one result for every post_id" if batch else "one result for the source item"
    return (
        "You classify public source items for an A-share KOL evidence audit. Do not give investment advice. "
        f"Return {target} as one valid JSON object and follow the supplied JSON Schema exactly. "
        "Separate original pre-event recommendations from retrospectives, holdings, analysis, secondhand "
        "content and promotion. For multi-stock recommendations emit one draft per stock. Evidence spans "
        "must be exact substrings from text, article_text, quoted_text or ocr_text. Never invent symbols, "
        "reasons, prices, time horizons or conditions. Preserve explicit entry conditions. If a stock list "
        "has no individual reason, state in Chinese that it was listed for the day and no stock-specific "
        "reason was provided. Do not emit recommendation drafts for themes, industries, concepts or market "
        "indexes, including provider-specific 88xxxx theme index codes; keep those as analysis only. "
        "Chinese source summaries and theses must remain concise Chinese.\n\n"
        "JSON Schema:\n" + schema
    )


def _parse_json_object_content(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("model content is not text")
    text = content.lstrip("\ufeff").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("model result is not an object")
    return value


class _DeepSeekClassifierBase:
    model_name = OPENCODE_GO_MODEL
    provider_name = "opencode-go"
    api_url = OPENCODE_GO_API_URL

    def __init__(
        self,
        schema_path: Path,
        credentials: DeepSeekCredentialStore | None = None,
        *,
        timeout_seconds: float = 180,
        client: httpx.Client | None = None,
    ):
        self.schema_path = Path(schema_path)
        self.credentials = credentials or DeepSeekCredentialStore()
        self.timeout_seconds = max(1, timeout_seconds)
        self.client = client

    def _request(self, user_payload: dict[str, Any], *, batch: bool) -> dict[str, Any]:
        schema = self.schema_path.read_text(encoding="utf-8")
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _deepseek_review_instruction(schema, batch=batch)},
                {"role": "user", "content": "Return JSON only.\n" + _json(user_payload)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        headers = opencode_go_headers(
            self.credentials.load(),
            f"kol-classifier:{'batch' if batch else 'single'}:{_json(user_payload)}",
        )
        try:
            if self.client is None:
                response = httpx.post(
                    self.api_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
            else:
                response = self.client.post(
                    self.api_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelProviderUnavailableError(f"OpenCode Go connection failed: {exc.__class__.__name__}") from exc
        if response.status_code in {401, 402, 403, 408, 409, 429} or response.status_code >= 500:
            raise ModelProviderUnavailableError(f"OpenCode Go API unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise RuntimeError(f"OpenCode Go request rejected (HTTP {response.status_code})")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            value = _parse_json_object_content(content)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenCode Go returned invalid JSON") from exc
        return value


class DeepSeekPostClassifier(_DeepSeekClassifierBase):
    prompt_version = "kol-post-opencode-go-v1"

    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        return validate_model_payload(self._request(_deepseek_source_item(post), batch=False))


class DeepSeekBatchPostClassifier(_DeepSeekClassifierBase):
    prompt_version = "kol-morning-opencode-go-batch-v1"
    request_batch_size = 3

    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not 1 <= len(posts) <= 10:
            raise ValueError("DeepSeek batches must contain between 1 and 10 posts")
        self.last_errors: dict[str, str] = {}
        results: dict[str, dict[str, Any]] = {}
        for index in range(0, len(posts), self.request_batch_size):
            results.update(self._classify_chunk(posts[index:index + self.request_batch_size]))
        return results

    def _classify_chunk(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        requested = {str(post["post_id"]) for post in posts}
        try:
            raw = self._request({"posts": [_deepseek_source_item(post) for post in posts]}, batch=True)
        except ModelProviderUnavailableError:
            raise
        except Exception as exc:
            if len(posts) > 1:
                recovered: dict[str, dict[str, Any]] = {}
                for post in posts:
                    recovered.update(self._classify_chunk([post]))
                return recovered
            self.last_errors[str(posts[0]["post_id"])] = str(exc)
            return {}

        results: dict[str, dict[str, Any]] = {}
        retry_ids: set[str] = set()
        raw_results = raw.get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
            retry_ids.update(requested)
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            post_id = str(item.get("post_id") or "")
            if post_id not in requested or post_id in results:
                continue
            try:
                results[post_id] = validate_model_payload(
                    {key: value for key, value in item.items() if key != "post_id"}
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                self.last_errors[post_id] = str(exc)
                retry_ids.add(post_id)

        retry_ids.update(requested - set(results))
        if retry_ids and len(posts) > 1:
            posts_by_id = {str(post["post_id"]): post for post in posts}
            for post_id in sorted(retry_ids):
                results.update(self._classify_chunk([posts_by_id[post_id]]))
        elif retry_ids:
            post_id = next(iter(retry_ids))
            self.last_errors.setdefault(post_id, "DeepSeek omitted this post")
        return results


def _is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, (ModelProviderUnavailableError, FileNotFoundError)):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    lowered = str(exc).lower()
    return any(marker in lowered for marker in (
        "timed out",
        "connection",
        "temporarily unavailable",
        "command not found",
        "not recognized",
        "cannot find the file",
    ))


class _FallbackClassifierBase:
    def __init__(self, primary: Any, backup: Any):
        self.primary = primary
        self.backup = backup
        self.last_provider = str(getattr(primary, "model_name", primary.__class__.__name__))
        self.last_fallback_reason = ""
        self.primary_available = True

    @property
    def prompt_version(self) -> str:
        selected = self.backup if self.last_provider == getattr(self.backup, "model_name", "") else self.primary
        return str(getattr(selected, "prompt_version", ""))

    @property
    def model_name(self) -> str:
        return self.last_provider

    @property
    def timeout_seconds(self) -> float:
        return float(getattr(self.primary, "timeout_seconds", 240))

    @timeout_seconds.setter
    def timeout_seconds(self, value: float) -> None:
        if hasattr(self.primary, "timeout_seconds"):
            self.primary.timeout_seconds = value
        if hasattr(self.backup, "timeout_seconds"):
            self.backup.timeout_seconds = value

    def _fallback(self, exc: Exception) -> bool:
        if not _is_transient_model_error(exc):
            return False
        self.last_fallback_reason = str(exc)[:500]
        self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
        self.primary_available = False
        return True


class FallbackPostClassifier(_FallbackClassifierBase):
    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        if not self.primary_available:
            self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
            return self.backup.classify(post)
        self.last_provider = str(getattr(self.primary, "model_name", self.primary.__class__.__name__))
        try:
            return self.primary.classify(post)
        except Exception as exc:
            if not self._fallback(exc):
                raise
        return self.backup.classify(post)


class FallbackBatchPostClassifier(_FallbackClassifierBase):
    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.primary_available:
            self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
            return self.backup.classify_many(posts)
        self.last_provider = str(getattr(self.primary, "model_name", self.primary.__class__.__name__))
        try:
            return self.primary.classify_many(posts)
        except Exception as exc:
            if not self._fallback(exc):
                raise
        return self.backup.classify_many(posts)


def build_post_classifier(
    schema_path: Path,
    workspace: Path,
    *,
    deepseek_credentials: DeepSeekCredentialStore | None = None,
) -> FallbackPostClassifier:
    return FallbackPostClassifier(
        CodexPostClassifier(schema_path, workspace),
        DeepSeekPostClassifier(schema_path, deepseek_credentials),
    )


def build_batch_post_classifier(
    schema_path: Path,
    workspace: Path,
    *,
    deepseek_credentials: DeepSeekCredentialStore | None = None,
) -> FallbackBatchPostClassifier:
    return FallbackBatchPostClassifier(
        CodexBatchPostClassifier(schema_path, workspace),
        DeepSeekBatchPostClassifier(schema_path, deepseek_credentials),
    )

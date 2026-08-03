from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


QUESTION_RE = re.compile(r"https?://(?:www\.)?zhihu\.com/question/(\d+)(?:[/?#].*)?$", re.I)
ANSWER_RE = re.compile(r"https?://(?:www\.)?zhihu\.com/question/(\d+)/answer/(\d+)(?:[/?#].*)?$", re.I)
PIN_RE = re.compile(r"https?://(?:www\.)?zhihu\.com/pin/(\d+)(?:[/?#].*)?$", re.I)


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Capture Zhihu question, answer, or pin pages through a local Chrome/Edge page."
    )
    parser.add_argument("--url", required=True, help="Zhihu question, answer, or pin URL.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--port", type=int, default=9222, help="Browser remote debugging port.")
    parser.add_argument("--browser", default="auto", choices=["auto", "chrome", "edge"], help="Browser to launch.")
    parser.add_argument("--browser-path", default="", help="Browser executable path.")
    parser.add_argument("--chrome-path", default="", help="Legacy alias for --browser-path.")
    parser.add_argument("--profile-directory", default="Profile 2", help="Browser profile directory name.")
    parser.add_argument("--user-data-dir", default="", help="Dedicated browser user data directory for DevTools.")
    parser.add_argument("--no-launch", action="store_true", help="Do not try to launch the browser if CDP is unavailable.")
    parser.add_argument("--wait", type=int, default=30, help="Seconds to wait for browser/CDP and page loading.")
    parser.add_argument("--max-answers", type=int, default=12, help="Maximum answers to collect.")
    parser.add_argument("--max-scrolls", type=int, default=10, help="Maximum viewport scrolls for DOM fallback.")
    parser.add_argument("--max-chars", type=int, default=12000, help="Maximum content characters in output.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    browser_path = args.browser_path or args.chrome_path or default_browser_path(args.browser)
    result = capture_zhihu(
        url=args.url,
        port=args.port,
        chrome_path=browser_path,
        profile_directory=args.profile_directory,
        user_data_dir=args.user_data_dir,
        launch=not args.no_launch,
        wait_seconds=args.wait,
        max_answers=args.max_answers,
        max_scrolls=args.max_scrolls,
        max_chars=args.max_chars,
    )

    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("content", "") or result.get("error", ""))
    return 0 if result.get("ok") else 1


def configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def capture_zhihu(
    *,
    url: str,
    port: int,
    chrome_path: str,
    profile_directory: str,
    user_data_dir: str,
    launch: bool,
    wait_seconds: int,
    max_answers: int,
    max_scrolls: int,
    max_chars: int,
) -> dict[str, Any]:
    if not is_supported_zhihu_url(url):
        return error_result(url, "unsupported Zhihu URL; use /question/<id>, /question/<id>/answer/<id>, or /pin/<id>")

    endpoint = wait_for_cdp(port, seconds=2)
    if not endpoint and launch:
        launch_chrome(chrome_path, profile_directory, user_data_dir, port, url)
        endpoint = wait_for_cdp(port, seconds=wait_seconds)
    if not endpoint:
        user_data_arg = f' --user-data-dir="{user_data_dir}"' if user_data_dir else ""
        return error_result(
            url,
            (
                "Browser DevTools is not reachable. Close Chrome/Edge and rerun, or start the browser with "
                f'"{chrome_path}" --remote-debugging-port={port}{user_data_arg} --profile-directory="{profile_directory}"'
            ),
        )

    try:
        tab = open_or_find_tab(port, url)
        with CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": url})
            wait_for_page(cdp, wait_seconds)
            title = read_title(cdp) or "Zhihu capture"

            if pin_id_from_url(url):
                api_result = collect_pin_from_page_api(cdp)
                if api_result.get("ok") and api_result.get("answers"):
                    answers = api_result["answers"][:1]
                    method = "zhihu-local-browser:pin-api"
                else:
                    dom_result = collect_pin_from_dom(cdp)
                    answers = dom_result["answers"][:1]
                    method = "zhihu-local-browser:pin-dom"
            else:
                api_result = collect_answers_from_page_api(cdp, max_answers)
                if api_result.get("ok") and api_result.get("answers"):
                    answers = api_result["answers"][:max_answers]
                    method = "zhihu-local-browser:page-api"
                else:
                    dom_result = collect_answers_from_dom(cdp, max_answers, max_scrolls)
                    answers = dom_result["answers"][:max_answers]
                    method = "zhihu-local-browser:dom"

            if not answers:
                detail = api_result.get("error") if isinstance(api_result, dict) else ""
                page_issue = diagnose_page_issue(cdp)
                return error_result(url, detail or page_issue or "no content found in local browser page", method=method)

            content = format_markdown(title, url, answers, method)[:max_chars]
            return {
                "ok": True,
                "url": url,
                "title": title[:160],
                "method": method,
                "content": content,
                "summary": f"Captured {len(answers)} Zhihu answer(s) via local browser.",
                "answers": answers,
                "answer_count": len(answers),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            }
    except Exception as exc:
        return error_result(url, f"{type(exc).__name__}: {exc}", method="zhihu-local-browser")


def is_supported_zhihu_url(url: str) -> bool:
    return bool(QUESTION_RE.match(url) or ANSWER_RE.match(url) or PIN_RE.match(url))


def question_id_from_url(url: str) -> str:
    answer = ANSWER_RE.match(url)
    if answer:
        return answer.group(1)
    question = QUESTION_RE.match(url)
    return question.group(1) if question else ""


def answer_id_from_url(url: str) -> str:
    answer = ANSWER_RE.match(url)
    return answer.group(2) if answer else ""


def pin_id_from_url(url: str) -> str:
    pin = PIN_RE.match(url)
    return pin.group(1) if pin else ""


def default_chrome_path() -> str:
    return default_browser_path("chrome")


def default_browser_path(browser: str = "auto") -> str:
    browser = (browser or "auto").lower()
    candidates: list[str] = []
    if browser in {"auto", "edge"}:
        candidates.extend(
            [
                os.environ.get("EDGE_PATH", ""),
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
        )
    if browser in {"auto", "chrome"}:
        candidates.extend(
            [
                os.environ.get("CHROME_PATH", ""),
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        )
    fallback = "msedge.exe" if browser == "edge" else "chrome.exe"
    if browser == "auto":
        fallback = "msedge.exe"
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return fallback


def wait_for_cdp(port: int, seconds: int) -> dict[str, Any] | None:
    deadline = time.time() + max(seconds, 0)
    while time.time() <= deadline:
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
        except Exception:
            time.sleep(0.5)
    return None


def launch_chrome(chrome_path: str, profile_directory: str, user_data_dir: str, port: int, url: str) -> None:
    command = [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
    ]
    if user_data_dir:
        data_dir = Path(user_data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        command.append(f"--user-data-dir={data_dir}")
    command.extend(
        [
            f"--profile-directory={profile_directory}",
            "--new-window",
            url,
        ]
    )
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_or_find_tab(port: int, url: str) -> dict[str, Any]:
    qid = question_id_from_url(url)
    pin_id = pin_id_from_url(url)
    tabs = http_json(f"http://127.0.0.1:{port}/json/list", timeout=5)
    for tab in tabs:
        tab_url = str(tab.get("url", ""))
        if qid and f"zhihu.com/question/{qid}" in tab_url and tab.get("webSocketDebuggerUrl"):
            return tab
        if pin_id and f"zhihu.com/pin/{pin_id}" in tab_url and tab.get("webSocketDebuggerUrl"):
            return tab

    quoted = urllib.parse.quote(url, safe="")
    try:
        return http_json(f"http://127.0.0.1:{port}/json/new?{quoted}", timeout=5, method="PUT")
    except urllib.error.HTTPError:
        tab = http_json(f"http://127.0.0.1:{port}/json/new", timeout=5, method="PUT")
        if tab.get("webSocketDebuggerUrl"):
            with CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
                cdp.call("Page.navigate", {"url": url})
        return tab


def http_json(url: str, timeout: int, method: str = "GET") -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


class CdpClient:
    def __init__(self, websocket_url: str):
        parsed = urllib.parse.urlparse(websocket_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path
        if parsed.query:
            self.path += "?" + parsed.query
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self) -> "CdpClient":
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self.sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("CDP WebSocket handshake failed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        self.send_json({"id": message_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.recv_json(deadline - time.time())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"CDP {method} timed out")

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_frame(data)

    def recv_json(self, timeout: float) -> dict[str, Any]:
        text = self.recv_frame(timeout)
        return json.loads(text)

    def send_frame(self, data: bytes) -> None:
        if not self.sock:
            raise RuntimeError("WebSocket is not connected")
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_frame(self, timeout: float) -> str:
        if not self.sock:
            raise RuntimeError("WebSocket is not connected")
        self.sock.settimeout(max(timeout, 0.1))
        while True:
            first = recv_exact(self.sock, 2)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", recv_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", recv_exact(self.sock, 8))[0]
            mask = recv_exact(self.sock, 4) if masked else b""
            payload = recv_exact(self.sock, length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by Chrome")
            if opcode == 0x9:
                self.send_pong(payload)

    def send_pong(self, payload: bytes) -> None:
        if not self.sock:
            return
        header = bytearray([0x8A])
        length = len(payload)
        header.append(0x80 | length)
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def wait_for_page(cdp: CdpClient, seconds: int) -> None:
    deadline = time.time() + seconds
    stable_seen = 0
    while time.time() < deadline:
        state = evaluate(
            cdp,
            "({readyState: document.readyState, title: document.title, body: !!document.body, text: document.body ? document.body.innerText.length : 0})",
            timeout=5,
        )
        if state.get("body") and state.get("text", 0) > 200:
            stable_seen += 1
            if stable_seen >= 2:
                return
        time.sleep(1)


def read_title(cdp: CdpClient) -> str:
    value = evaluate(cdp, "document.title || ''", timeout=5)
    return str(value).replace(" - 知乎", "").strip()


def diagnose_page_issue(cdp: CdpClient) -> str:
    text = str(
        evaluate(
            cdp,
            "(document.body && document.body.innerText || '').slice(0, 2000)",
            timeout=5,
        )
        or ""
    )
    if any(marker in text for marker in ["登录", "注册", "请登录", "验证码", "安全验证", "请完成验证"]):
        return "local browser opened but Zhihu needs login or verification; finish it in the opened browser and resend the link"
    return ""


def evaluate(cdp: CdpClient, expression: str, timeout: int = 20, await_promise: bool = False) -> Any:
    result = cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "timeout": timeout * 1000,
        },
        timeout=timeout + 2,
    )
    remote = result.get("result", {})
    if "value" in remote:
        return remote["value"]
    if "description" in remote:
        return remote["description"]
    return None


def collect_pin_from_page_api(cdp: CdpClient) -> dict[str, Any]:
    expression = r"""
(async () => {
  const match = location.href.match(/pin\/(\d+)/);
  if (!match) return {ok: false, error: 'missing pin id'};
  const pinId = match[1];
  const response = await fetch(`/api/v4/pins/${pinId}`, {credentials: 'include', headers: {accept: 'application/json'}});
  const text = await response.text();
  if (!response.ok) return {ok: false, status: response.status, error: text.slice(0, 500), answers: []};
  let row;
  try { row = JSON.parse(text); } catch (err) { return {ok: false, error: String(err), answers: []}; }
  const div = document.createElement('div');
  const raw = row.content || row.excerpt || row.title || row.description || row.comment_content || '';
  div.innerHTML = typeof raw === 'string' ? raw : JSON.stringify(raw);
  const clean = (div.innerText || div.textContent || '').replace(/\s+\n/g, '\n').replace(/\n\s+/g, '\n').trim();
  const author = row.author || row.people || row.user || {};
  const item = {
    id: String(row.id || pinId),
    author: author.name || author.fullname || '',
    author_username: author.url_token || author.id || '',
    url: location.href,
    voteup_count: row.voteup_count || row.reaction_count || 0,
    comment_count: row.comment_count || 0,
    text: clean.slice(0, 6000)
  };
  return {ok: item.text.length > 20, answers: item.text ? [item] : []};
})()
"""
    value = evaluate(cdp, expression, timeout=20, await_promise=True)
    return value if isinstance(value, dict) else {"ok": False, "error": "unexpected pin API result", "answers": []}


def collect_pin_from_dom(cdp: CdpClient) -> dict[str, Any]:
    value = evaluate(cdp, PIN_DOM_EXTRACT_SCRIPT, timeout=10)
    if not isinstance(value, dict):
        return {"ok": False, "answers": []}
    text = normalize_text(str(value.get("text", "")))
    if len(text) < 40:
        return {"ok": False, "answers": []}
    return {
        "ok": True,
        "answers": [
            {
                "author": value.get("author", ""),
                "url": value.get("url", ""),
                "vote_text": value.get("vote_text", ""),
                "text": text[:6000],
            }
        ],
    }

def collect_answers_from_page_api(cdp: CdpClient, max_answers: int) -> dict[str, Any]:
    expression = f"""
(async () => {{
  const match = location.href.match(/question\\/(\\d+)/);
  if (!match) return {{ok: false, error: 'missing question id'}};
  const qid = match[1];
  const answerMatch = location.href.match(/question\\/\\d+\\/answer\\/(\\d+)/);
  const answerId = answerMatch ? answerMatch[1] : '';
  const listInclude = encodeURIComponent('data[*].is_normal,comment_count,content,excerpt,voteup_count,created_time,updated_time,author.name,author.url_token,question.title');
  const answerInclude = encodeURIComponent('content,excerpt,voteup_count,comment_count,created_time,updated_time,author.name,author.url_token,question.title');
  const answers = [];
  const toAnswer = (row) => {{
    const div = document.createElement('div');
    div.innerHTML = row.content || row.excerpt || '';
    const clean = (div.innerText || div.textContent || '').replace(/\\s+\\n/g, '\\n').replace(/\\n\\s+/g, '\\n').trim();
    return {{
      id: String(row.id || ''),
      author: row.author && row.author.name ? row.author.name : '',
      author_username: row.author && row.author.url_token ? row.author.url_token : '',
      url: row.id ? `https://www.zhihu.com/question/${{qid}}/answer/${{row.id}}` : location.href,
      voteup_count: row.voteup_count || 0,
      comment_count: row.comment_count || 0,
      created_time: row.created_time || 0,
      updated_time: row.updated_time || 0,
      text: clean.slice(0, 6000)
    }};
  }};
  if (answerId) {{
    const api = `/api/v4/answers/${{answerId}}?include=${{answerInclude}}`;
    const response = await fetch(api, {{credentials: 'include', headers: {{accept: 'application/json'}}}});
    const text = await response.text();
    if (!response.ok) return {{ok: false, status: response.status, error: text.slice(0, 500), answers}};
    let row;
    try {{ row = JSON.parse(text); }} catch (err) {{ return {{ok: false, error: String(err), answers}}; }}
    if (row && (row.content || row.excerpt)) answers.push(toAnswer(row));
    return {{ok: answers.length > 0, answers}};
  }}
  let offset = 0;
  const limit = Math.min(20, Math.max(5, {int(max_answers)}));
  for (let page = 0; page < 4 && answers.length < {int(max_answers)}; page++) {{
    const api = `/api/v4/questions/${{qid}}/answers?include=${{listInclude}}&limit=${{limit}}&offset=${{offset}}&platform=desktop&sort_by=default`;
    const response = await fetch(api, {{credentials: 'include', headers: {{accept: 'application/json'}}}});
    const text = await response.text();
    if (!response.ok) return {{ok: false, status: response.status, error: text.slice(0, 500), answers}};
    let payload;
    try {{ payload = JSON.parse(text); }} catch (err) {{ return {{ok: false, error: String(err), answers}}; }}
    const rows = Array.isArray(payload.data) ? payload.data : [];
    for (const row of rows) {{
      answers.push(toAnswer(row));
      if (answers.length >= {int(max_answers)}) break;
    }}
    if (!payload.paging || payload.paging.is_end) break;
    offset += limit;
  }}
  return {{ok: answers.length > 0, answers}};
}})()
"""
    value = evaluate(cdp, expression, timeout=25, await_promise=True)
    return value if isinstance(value, dict) else {"ok": False, "error": "unexpected API result"}


def collect_answers_from_dom(cdp: CdpClient, max_answers: int, max_scrolls: int) -> dict[str, Any]:
    answers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(max_scrolls + 1):
        value = evaluate(cdp, DOM_EXTRACT_SCRIPT, timeout=10)
        for answer in value.get("answers", []) if isinstance(value, dict) else []:
            text = normalize_text(str(answer.get("text", "")))
            if len(text) < 80:
                continue
            key = answer.get("url") or hashlib.sha1(text[:500].encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            answer["text"] = text[:6000]
            answers.append(answer)
            if len(answers) >= max_answers:
                return {"ok": True, "answers": answers}
        evaluate(cdp, "window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * 0.9))); true", timeout=5)
        time.sleep(1.2)
    return {"ok": bool(answers), "answers": answers}


PIN_DOM_EXTRACT_SCRIPT = r"""
(() => {
  const root = document.querySelector('.PinItem, .ContentItem, [data-zop], main, article') || document.body;
  const authorNode = root.querySelector('.AuthorInfo-name, .UserLink-link, [itemprop="author"]');
  const voteNode = root.querySelector('.VoteButton, .ContentItem-actions button');
  const text = (root.innerText || document.body.innerText || '').trim();
  return {
    author: authorNode ? authorNode.innerText.trim() : '',
    url: location.href,
    vote_text: voteNode ? voteNode.innerText.trim() : '',
    text: text.slice(0, 7000),
    title: document.title
  };
})()
"""

DOM_EXTRACT_SCRIPT = r"""
(() => {
  const selectors = [
    '.List-item',
    '.AnswerItem',
    '.ContentItem',
    '[itemprop="answer"]',
    '[data-zop]'
  ];
  const cards = Array.from(document.querySelectorAll(selectors.join(',')));
  const answers = [];
  for (const card of cards) {
    const text = (card.innerText || '').trim();
    if (text.length < 80) continue;
    const authorNode = card.querySelector('.AuthorInfo-name, .UserLink-link, [itemprop="author"]');
    const linkNode = card.querySelector('a[href*="/answer/"]');
    const voteNode = card.querySelector('.VoteButton, .ContentItem-actions button');
    answers.push({
      author: authorNode ? authorNode.innerText.trim() : '',
      url: linkNode ? linkNode.href : location.href,
      vote_text: voteNode ? voteNode.innerText.trim() : '',
      text: text.slice(0, 7000)
    });
  }
  return {answers, scrollY: window.scrollY, title: document.title};
})()
"""


def normalize_text(value: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def format_markdown(title: str, url: str, answers: list[dict[str, Any]], method: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"Source: {url}",
        f"Method: {method}",
        f"Captured at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"## Answers ({len(answers)})",
    ]
    for index, answer in enumerate(answers, start=1):
        author = answer.get("author") or "Unknown author"
        lines.extend(["", f"### {index}. {author}"])
        meta = []
        if answer.get("url"):
            meta.append(f"URL: {answer['url']}")
        if answer.get("voteup_count"):
            meta.append(f"Voteup: {answer['voteup_count']}")
        if answer.get("comment_count"):
            meta.append(f"Comments: {answer['comment_count']}")
        if answer.get("vote_text"):
            meta.append(f"Vote text: {answer['vote_text']}")
        if meta:
            lines.append(" | ".join(meta))
            lines.append("")
        lines.append(normalize_text(str(answer.get("text", ""))))
    return "\n".join(lines).strip() + "\n"


def error_result(url: str, error: str, method: str = "zhihu-local-browser") -> dict[str, Any]:
    return {
        "ok": False,
        "url": url,
        "method": method,
        "content": "",
        "summary": "",
        "answers": [],
        "answer_count": 0,
        "error": error,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    sys.exit(main())







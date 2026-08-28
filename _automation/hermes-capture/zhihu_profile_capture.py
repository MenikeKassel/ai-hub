from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import zhihu_local_capture as local


def main() -> int:
    local.configure_stdio()
    parser = argparse.ArgumentParser(
        description="Fetch a Zhihu member's answers through an authenticated local browser."
    )
    parser.add_argument("--handle", required=True, help="Zhihu member url_token.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--browser", default="edge", choices=["auto", "chrome", "edge"])
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--profile-directory", default="Default")
    parser.add_argument("--user-data-dir", default="")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Ensure the browser DevTools endpoint is available without opening a profile tab.",
    )
    parser.add_argument("--wait", type=int, default=30)
    parser.add_argument("--max-chars", type=int, default=80000)
    args = parser.parse_args()

    capture_kwargs = {
        "handle": args.handle,
        "limit": args.limit,
        "port": args.port,
        "browser_path": args.browser_path or local.default_browser_path(args.browser),
        "profile_directory": args.profile_directory,
        "user_data_dir": args.user_data_dir,
        "launch": not args.no_launch,
        "wait_seconds": args.wait,
        "max_chars": args.max_chars,
    }
    result = (
        prepare_browser(
            port=args.port,
            browser_path=capture_kwargs["browser_path"],
            profile_directory=args.profile_directory,
            user_data_dir=args.user_data_dir,
            profile_url=f"https://www.zhihu.com/people/{urllib.parse.quote(args.handle.strip().strip('/'))}/answers",
            launch=not args.no_launch,
            wait_seconds=args.wait,
        )
        if args.prepare_only
        else capture_profile_answers(**capture_kwargs)
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def capture_profile_answers(
    *,
    handle: str,
    limit: int,
    port: int,
    browser_path: str,
    profile_directory: str,
    user_data_dir: str,
    launch: bool,
    wait_seconds: int,
    max_chars: int,
) -> dict[str, Any]:
    clean_handle = handle.strip().strip("/")
    if not clean_handle or len(clean_handle) > 100:
        return {"ok": False, "error": "invalid Zhihu member handle", "posts": []}
    profile_url = f"https://www.zhihu.com/people/{urllib.parse.quote(clean_handle)}/answers"
    prepared = prepare_browser(
        port=port,
        browser_path=browser_path,
        profile_directory=profile_directory,
        user_data_dir=user_data_dir,
        profile_url=profile_url,
        launch=launch,
        wait_seconds=wait_seconds,
    )
    if not prepared.get("ok"):
        return {**prepared, "posts": []}

    tab: dict[str, Any] | None = None
    try:
        tab = open_or_find_profile_tab(port, clean_handle, profile_url)
        with local.CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": profile_url})
            local.wait_for_page(cdp, wait_seconds)
            issue = local.diagnose_page_issue(cdp)
            if issue:
                return {"ok": False, "error": issue, "posts": []}
            payload = fetch_answers(cdp, clean_handle, limit, max_chars)
            if not payload.get("ok"):
                return {
                    "ok": False,
                    "error": str(payload.get("error") or "Zhihu profile API failed"),
                    "posts": [],
                }
            return {
                "ok": True,
                "provider": "zhihu-local",
                "handle": clean_handle,
                "profile_url": profile_url,
                "posts": payload.get("posts", []),
                "paging": payload.get("paging", {}),
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "posts": [],
        }
    finally:
        if tab:
            close_tab(port, tab)


def prepare_browser(
    *,
    port: int,
    browser_path: str,
    profile_directory: str,
    user_data_dir: str,
    profile_url: str,
    launch: bool,
    wait_seconds: int,
) -> dict[str, Any]:
    """Ensure one shared browser session is ready for a whole Zhihu batch."""
    endpoint = local.wait_for_cdp(port, seconds=2)
    if not endpoint and launch:
        # Keep a blank tab alive for the whole batch. Closing the only
        # profile tab makes Chrome exit, so subsequent accounts falsely
        # report that DevTools is unavailable.
        local.launch_chrome(
            browser_path,
            profile_directory,
            user_data_dir,
            port,
            "about:blank",
        )
        endpoint = local.wait_for_cdp(port, seconds=wait_seconds)
    if not endpoint:
        return {
            "ok": False,
            "error": "Zhihu browser DevTools is unavailable",
            "port": port,
        }
    return {"ok": True, "port": port, "endpoint": endpoint}


def open_or_find_profile_tab(port: int, handle: str, url: str) -> dict[str, Any]:
    tabs = local.http_json(f"http://127.0.0.1:{port}/json/list", timeout=5)
    marker = f"zhihu.com/people/{handle}"
    for tab in tabs:
        if marker in str(tab.get("url") or "") and tab.get("webSocketDebuggerUrl"):
            return tab
    quoted = urllib.parse.quote(url, safe="")
    return local.http_json(f"http://127.0.0.1:{port}/json/new?{quoted}", timeout=5, method="PUT")


def close_tab(port: int, tab: dict[str, Any]) -> None:
    tab_id = str(tab.get("id") or "").strip()
    if not tab_id:
        return
    try:
        local.http_json(f"http://127.0.0.1:{port}/json/close/{urllib.parse.quote(tab_id)}", timeout=3)
    except Exception:
        pass


def fetch_answers(
    cdp: local.CdpClient,
    handle: str,
    limit: int,
    max_chars: int,
) -> dict[str, Any]:
    requested = max(1, min(int(limit), 200))
    expression = PROFILE_FETCH_SCRIPT.replace("__HANDLE__", json.dumps(handle)).replace(
        "__LIMIT__", str(requested)
    ).replace("__MAX_CHARS__", str(max(1000, min(int(max_chars), 200000))))
    value = local.evaluate(cdp, expression, timeout=45, await_promise=True)
    return value if isinstance(value, dict) else {
        "ok": False,
        "error": "unexpected Zhihu profile API result",
        "posts": [],
    }


PROFILE_FETCH_SCRIPT = r"""
(async () => {
  const handle = __HANDLE__;
  const requested = __LIMIT__;
  const maxChars = __MAX_CHARS__;
  const include = encodeURIComponent(
    'data[*].is_normal,comment_count,content,excerpt,voteup_count,created_time,' +
    'updated_time,author.name,author.url_token,question.id,question.title'
  );
  const posts = [];
  let offset = 0;
  let paging = {};
  const cleanHtml = (html) => {
    const root = document.createElement('div');
    root.innerHTML = html || '';
    const blocks = Array.from(root.querySelectorAll('p,h1,h2,h3,h4,li,blockquote'))
      .map((node) => (node.innerText || node.textContent || '').trim())
      .filter(Boolean);
    const text = blocks.length ? blocks.join('\n') : (root.innerText || root.textContent || '');
    return text.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n')
      .replace(/\n{3,}/g, '\n\n').trim();
  };
  while (posts.length < requested) {
    const pageLimit = Math.min(20, requested - posts.length);
    const api = `/api/v4/members/${encodeURIComponent(handle)}/answers?include=${include}` +
      `&offset=${offset}&limit=${pageLimit}&sort_by=created`;
    const response = await fetch(api, {
      credentials: 'include',
      headers: {accept: 'application/json'}
    });
    const raw = await response.text();
    if (!response.ok) {
      return {ok: false, status: response.status, error: raw.slice(0, 500), posts};
    }
    let payload;
    try { payload = JSON.parse(raw); } catch (error) {
      return {ok: false, error: String(error), posts};
    }
    const rows = Array.isArray(payload.data) ? payload.data : [];
    for (const row of rows) {
      const question = row.question || {};
      const author = row.author || {};
      const text = cleanHtml(row.content || row.excerpt || '').slice(0, maxChars);
      if (!row.id || !question.id || !text) continue;
      posts.push({
        id: String(row.id),
        text,
        articleTitle: String(question.title || ''),
        url: `https://www.zhihu.com/question/${question.id}/answer/${row.id}`,
        author: {
          name: String(author.name || handle),
          screenName: String(author.url_token || handle)
        },
        createdAtISO: row.created_time ? new Date(row.created_time * 1000).toISOString() : '',
        updatedAtISO: row.updated_time ? new Date(row.updated_time * 1000).toISOString() : '',
        metrics: {
          likes: Number(row.voteup_count || 0),
          comments: Number(row.comment_count || 0)
        },
        lang: 'zh-CN'
      });
      if (posts.length >= requested) break;
    }
    paging = payload.paging || {};
    if (!rows.length || paging.is_end) break;
    offset += rows.length;
  }
  return {ok: true, posts, paging};
})()
"""


if __name__ == "__main__":
    sys.exit(main())

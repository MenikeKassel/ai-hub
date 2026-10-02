from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable

import zhihu_local_capture as local


SUPPORTED_SURFACES = ("answers", "articles", "ideas")
SURFACE_ALIASES = {
    "answer": "answers",
    "answers": "answers",
    "article": "articles",
    "articles": "articles",
    "idea": "ideas",
    "ideas": "ideas",
    "pin": "ideas",
    "pins": "ideas",
}
DEFAULT_SURFACES = SUPPORTED_SURFACES


def normalise_surfaces(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return stable, de-duplicated surface names for CLI/API callers."""
    if value is None:
        return DEFAULT_SURFACES
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    else:
        values = list(value)
    result: list[str] = []
    unsupported: list[str] = []
    for raw in values:
        name = str(raw or "").strip().casefold()
        if not name:
            continue
        if name == "all":
            for surface in SUPPORTED_SURFACES:
                if surface not in result:
                    result.append(surface)
            continue
        canonical = SURFACE_ALIASES.get(name)
        if canonical and canonical not in result:
            result.append(canonical)
        elif not canonical:
            unsupported.append(name)
    if unsupported:
        raise ValueError(
            "unsupported Zhihu profile surface(s): " + ", ".join(sorted(set(unsupported)))
        )
    return tuple(result or DEFAULT_SURFACES)


def main() -> int:
    local.configure_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Fetch bounded Zhihu member profile feeds through one authenticated "
            "local Chrome profile."
        )
    )
    parser.add_argument("--handle", required=True, help="Zhihu member url_token.")
    parser.add_argument(
        "--surfaces",
        default=",".join(DEFAULT_SURFACES),
        help="Comma-separated profile feeds: answers, articles, ideas (default: all).",
    )
    parser.add_argument(
        "--surface",
        action="append",
        dest="surface_values",
        default=[],
        help="Add one profile feed; may be repeated (alias for --surfaces).",
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum rows per surface.")
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--browser", default="chrome", choices=["auto", "chrome", "edge"])
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
    parser.add_argument(
        "--surface-budget-seconds",
        type=int,
        default=90,
        help="Bound all requested surface API calls within this browser evaluation budget.",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=80000)
    args = parser.parse_args()

    try:
        surfaces = normalise_surfaces(args.surface_values or args.surfaces)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "posts": []}, ensure_ascii=False))
        return 1

    capture_kwargs = {
        "handle": args.handle,
        "limit": args.limit,
        "surfaces": surfaces,
        "port": args.port,
        "browser_path": args.browser_path or local.default_browser_path(args.browser),
        "profile_directory": args.profile_directory,
        "user_data_dir": args.user_data_dir,
        "launch": not args.no_launch,
        "wait_seconds": args.wait,
        "surface_budget_seconds": args.surface_budget_seconds,
        "request_timeout_seconds": args.request_timeout_seconds,
        "max_chars": args.max_chars,
    }
    result = (
        prepare_browser(
            port=args.port,
            browser_path=capture_kwargs["browser_path"],
            profile_directory=args.profile_directory,
            user_data_dir=args.user_data_dir,
            profile_url=(
                "https://www.zhihu.com/people/"
                f"{urllib.parse.quote(args.handle.strip().strip('/'))}/answers"
            ),
            launch=not args.no_launch,
            wait_seconds=args.wait,
        )
        if args.prepare_only
        else capture_profile(**capture_kwargs)
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
    """Compatibility wrapper for callers that explicitly request answers only."""
    return capture_profile(
        handle=handle,
        limit=limit,
        surfaces=("answers",),
        port=port,
        browser_path=browser_path,
        profile_directory=profile_directory,
        user_data_dir=user_data_dir,
        launch=launch,
        wait_seconds=wait_seconds,
        surface_budget_seconds=90,
        request_timeout_seconds=12,
        max_chars=max_chars,
    )


def capture_profile(
    *,
    handle: str,
    limit: int,
    surfaces: str | Iterable[str] | None = None,
    port: int,
    browser_path: str,
    profile_directory: str,
    user_data_dir: str,
    launch: bool,
    wait_seconds: int,
    surface_budget_seconds: int = 90,
    request_timeout_seconds: int = 12,
    max_chars: int,
) -> dict[str, Any]:
    try:
        requested_surfaces = normalise_surfaces(surfaces)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "posts": [],
            "coverage": {"status": "unsupported"},
        }

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
        return {**prepared, "posts": [], "coverage": {"status": "failed"}}

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
                return {
                    "ok": False,
                    "error": issue,
                    "posts": [],
                    "coverage": {"status": "failed"},
                }
            payload = (
                fetch_answers(
                    cdp,
                    clean_handle,
                    limit,
                    max_chars,
                    surface_budget_seconds=surface_budget_seconds,
                    request_timeout_seconds=request_timeout_seconds,
                )
                if requested_surfaces == ("answers",)
                else fetch_profile(
                    cdp,
                    clean_handle,
                    limit,
                    max_chars,
                    requested_surfaces,
                    surface_budget_seconds=surface_budget_seconds,
                    request_timeout_seconds=request_timeout_seconds,
                )
            )
            if not payload.get("ok"):
                return {
                    **payload,
                    "handle": clean_handle,
                    "profile_url": profile_url,
                }
            warnings = list(payload.get("warnings") or [])
            if len(requested_surfaces) == 1:
                warnings.append(
                    "coverage_incomplete:only_" + requested_surfaces[0] + "_surface_requested"
                )
            return {
                "ok": True,
                "provider": "zhihu-local",
                "handle": clean_handle,
                "profile_url": profile_url,
                "posts": payload.get("posts", []),
                "surfaces": payload.get("surfaces", {}),
                "paging": payload.get("paging", {}),
                "coverage": payload.get("coverage", {}),
                "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "posts": [],
            "coverage": {"status": "failed"},
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
    """Ensure one shared Chrome session is ready for a whole Zhihu batch."""
    endpoint = local.wait_for_cdp(port, seconds=2)
    if not endpoint and launch:
        # Keep a blank tab alive for the entire batch.  Closing the only tab
        # makes Chrome exit and would cause the next KOL to start a second
        # browser process.  user_data_dir remains the caller's isolated profile.
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
    *,
    surface_budget_seconds: int = 90,
    request_timeout_seconds: int = 12,
) -> dict[str, Any]:
    """Compatibility helper retained for tests and older local callers."""
    return fetch_profile(
        cdp,
        handle,
        limit,
        max_chars,
        ("answers",),
        surface_budget_seconds=surface_budget_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


def fetch_profile(
    cdp: local.CdpClient,
    handle: str,
    limit: int,
    max_chars: int,
    surfaces: Iterable[str] = DEFAULT_SURFACES,
    *,
    surface_budget_seconds: int = 90,
    request_timeout_seconds: int = 12,
) -> dict[str, Any]:
    requested = max(1, min(int(limit), 1000))
    requested_surfaces = normalise_surfaces(surfaces)
    budget_seconds = max(10, min(int(surface_budget_seconds), 110))
    request_seconds = max(2, min(int(request_timeout_seconds), budget_seconds))
    expression = (
        PROFILE_FETCH_SCRIPT.replace("__HANDLE__", json.dumps(handle))
        .replace("__SURFACES__", json.dumps(list(requested_surfaces)))
        .replace("__LIMIT__", str(requested))
        .replace("__MAX_CHARS__", str(max(1000, min(int(max_chars), 200000))))
        .replace("__BUDGET_MS__", str(budget_seconds * 1000))
        .replace("__REQUEST_TIMEOUT_MS__", str(request_seconds * 1000))
    )
    # The JS itself has a hard budget.  Give CDP only a small teardown margin;
    # this keeps a three-surface run inside the provider's 120s subprocess limit.
    value = local.evaluate(
        cdp,
        expression,
        timeout=budget_seconds + 5,
        await_promise=True,
    )
    if isinstance(value, dict):
        return value
    return {
        "ok": False,
        "error": "unexpected Zhihu profile API result",
        "posts": [],
        "coverage": {"status": "failed"},
    }


PROFILE_FETCH_SCRIPT = r"""
(async () => {
  const handle = __HANDLE__;
  const requested = __LIMIT__;
  const wanted = __SURFACES__;
  const maxChars = __MAX_CHARS__;
  const deadline = Date.now() + __BUDGET_MS__;
  const requestTimeoutMs = __REQUEST_TIMEOUT_MS__;
  const specs = {
    answers: {
      endpoint: 'answers', type: 'answer',
      include: 'data[*].is_normal,comment_count,content,excerpt,voteup_count,created_time,updated_time,author.name,author.url_token,question.id,question.title'
    },
    articles: {
      endpoint: 'articles', type: 'article',
      include: 'data[*].title,content,excerpt,voteup_count,comment_count,created_time,updated_time,author.name,author.url_token'
    },
    ideas: {
      endpoint: 'pins', type: 'idea',
      include: 'data[*].content,excerpt,title,voteup_count,comment_count,created_time,updated_time,author.name,author.url_token'
    }
  };
  const surfaces = {};
  const posts = [];
  const seen = new Set();
  const warnings = [];
  const toHtmlString = (value) => {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') return toHtmlString(item.content || item.text || item.value || '');
      return '';
    }).filter(Boolean).join('\n');
    if (value && typeof value === 'object') return toHtmlString(value.content || value.text || value.value || '');
    return '';
  };
  const cleanHtml = (html) => {
    const root = document.createElement('div');
    root.innerHTML = toHtmlString(html);
    const blocks = Array.from(root.querySelectorAll('p,h1,h2,h3,h4,li,blockquote'))
      .map((node) => (node.innerText || node.textContent || '').trim())
      .filter(Boolean);
    const text = blocks.length ? blocks.join('\n') : (root.innerText || root.textContent || '');
    return text.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n')
      .replace(/\n{3,}/g, '\n\n').trim();
  };
  const sourceTime = (value) => {
    if (value === null || value === undefined || value === '' || value === 0) return '';
    if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))) {
      let number = Number(value);
      if (!Number.isFinite(number)) return '';
      if (number < 100000000000) number *= 1000;
      const parsed = new Date(number);
      return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString();
    }
    const parsed = new Date(String(value));
    return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString();
  };
  const authorPayload = (row) => {
    const author = row.author || row.people || row.user || {};
    const name = String(author.name || author.fullname || '');
    const screenName = String(author.url_token || author.screen_name || '');
    const matches = screenName ? screenName.toLowerCase() === String(handle).toLowerCase() : null;
    return {
      name, screenName,
      authorMatchesHandle: matches,
      authorMismatch: matches === false
    };
  };
  const toItem = (row, spec) => {
    const rawId = String(row.id || '');
    if (!/^\d+$/.test(rawId)) return null;
    const author = authorPayload(row);
    const question = row.question || {};
    const created = sourceTime(row.created_time !== undefined ? row.created_time : row.created);
    let url = '';
    let title = '';
    let raw = '';
    if (spec.type === 'answer') {
      if (!question.id) return null;
      url = `https://www.zhihu.com/question/${question.id}/answer/${rawId}`;
      title = String(question.title || '');
      raw = row.content || row.excerpt || '';
    } else if (spec.type === 'article') {
      url = `https://zhuanlan.zhihu.com/p/${rawId}`;
      title = String(row.title || row.article_title || '');
      raw = row.content || row.excerpt || row.description || '';
    } else {
      url = `https://www.zhihu.com/pin/${rawId}`;
      title = String(row.title || '');
      raw = row.content || row.excerpt || row.description || row.comment_content || '';
    }
    const text = cleanHtml(raw).slice(0, maxChars);
    if (!text) return null;
    return {
      id: rawId,
      type: spec.type,
      contentType: spec.type === 'idea' ? 'pin' : spec.type,
      surface: spec.type === 'answer' ? 'answers' : spec.type === 'article' ? 'articles' : 'ideas',
      text,
      articleTitle: title,
      url,
      author,
      requestedHandle: handle,
      createdAtISO: created,
      updatedAtISO: sourceTime(row.updated_time !== undefined ? row.updated_time : row.updated),
      metrics: {
        likes: Number(row.voteup_count || row.reaction_count || 0),
        comments: Number(row.comment_count || 0)
      },
      lang: 'zh-CN'
    };
  };
  const fetchOne = async (url) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) return {ok: false, budget: true, status: 0, error: 'surface budget exhausted'};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(requestTimeoutMs, remaining));
    try {
      const response = await fetch(url, {
        credentials: 'include',
        headers: {accept: 'application/json'},
        signal: controller.signal
      });
      const text = await response.text();
      return {ok: response.ok, status: response.status, text};
    } catch (error) {
      return {ok: false, status: 0, error: String(error)};
    } finally {
      clearTimeout(timer);
    }
  };
  for (const surface of wanted) {
    const spec = specs[surface];
    if (!spec) {
      surfaces[surface] = {status: 'unsupported', posts: [], warnings: ['unsupported_surface']};
      warnings.push(`surface:${surface}:unsupported`);
      continue;
    }
    const result = {status: 'failed', posts: [], warnings: [], pages: 0, exhausted: false, partial: false, bounded: false};
    let offset = 0;
    while (result.posts.length < requested && result.pages < 100) {
      let pageLimit = Math.min(20, requested - result.posts.length);
      const include = encodeURIComponent(spec.include);
      let response;
      let payload;
      let responseError = '';
      let retried10003 = false;
      for (let attempt = 0; attempt < 3; attempt++) {
        const api = `/api/v4/members/${encodeURIComponent(handle)}/${spec.endpoint}` +
          `?include=${include}&offset=${offset}&limit=${pageLimit}&sort_by=created`;
        response = await fetchOne(api);
        if (!response.ok) {
          responseError = response.status ? `http_${response.status}` : (response.error || 'request_failed');
          break;
        }
        try { payload = JSON.parse(response.text || '{}'); }
        catch (error) {
          responseError = 'invalid_json';
          break;
        }
        const errorObject = payload && typeof payload.error === 'object' ? payload.error : {};
        const errorText = JSON.stringify(payload && payload.error || '');
        const errorCode = String(payload && (payload.code || errorObject.code || '') || '');
        const is10003 = errorCode === '10003' || /10003/.test(errorText);
        if (is10003) {
          retried10003 = true;
          pageLimit = Math.max(1, Math.ceil(pageLimit / 2));
          if (attempt < 2) {
            const delayMs = Math.min(attempt === 0 ? 5000 : 15000, Math.max(0, deadline - Date.now()));
            await new Promise((resolve) => setTimeout(resolve, delayMs));
            continue;
          }
          responseError = 'code_10003_rate_limited';
          break;
        }
        const authOrRateCode = ['4041', '401', '403', '429'].find((code) => errorCode === code || errorText.includes(code));
        if (authOrRateCode) {
          responseError = `code_${authOrRateCode}`;
          break;
        }
        if (errorCode || (payload && payload.error)) {
          responseError = errorCode ? `code_${errorCode}` : 'api_error';
          break;
        }
        responseError = '';
        break;
      }
      if (responseError) {
        result.warnings.push(responseError);
        warnings.push(`surface:${surface}:${responseError}`);
        result.partial = result.pages > 0 || result.posts.length > 0;
        result.bounded = retried10003 || response && response.budget || Date.now() >= deadline;
        break;
      }
      const rows = Array.isArray(payload.data) ? payload.data : [];
      result.pages += 1;
      const countBefore = result.posts.length;
      let invalidRows = 0;
      for (const row of rows) {
        const item = toItem(row || {}, spec);
        if (!item) {
          result.warnings.push('row_missing_id_text_or_question');
          invalidRows += 1;
          continue;
        }
        if (!item.createdAtISO) {
          result.warnings.push('row_missing_created_at');
          invalidRows += 1;
        }
        if (!item.author.name && !item.author.screenName) {
          result.warnings.push('row_missing_author');
          invalidRows += 1;
        }
        if (item.author.authorMismatch) result.warnings.push('foreign_author_requires_verification');
        const key = `${item.type}:${item.id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        result.posts.push(item);
        posts.push(item);
        if (result.posts.length >= requested) break;
      }
      const paging = payload.paging || {};
      result.paging = paging;
      if (invalidRows > 0) result.partial = true;
      if (!rows.length || paging.is_end) {
        result.exhausted = true;
        break;
      }
      if (result.posts.length === countBefore) {
        result.warnings.push('pagination_stalled');
        warnings.push(`surface:${surface}:pagination_stalled`);
        result.partial = true;
        result.bounded = true;
        break;
      }
      offset += rows.length;
    }
    if (result.pages >= 100 && !result.exhausted) {
      result.warnings.push('pagination_page_cap');
      warnings.push(`surface:${surface}:pagination_page_cap`);
      result.bounded = true;
      result.partial = true;
    }
    if (result.pages > 0 && !result.partial) {
      result.status = result.posts.length ? 'success' : 'empty';
    } else if (result.partial && result.posts.length) {
      result.status = 'partial';
    }
    if (result.status === 'empty') {
      result.warnings.push('empty');
      warnings.push(`surface:${surface}:empty`);
    }
    if (result.status === 'success' && !result.exhausted && result.posts.length >= requested) {
      result.warnings.push('bounded_limit_reached');
      warnings.push(`surface:${surface}:bounded_limit_reached`);
      result.bounded = true;
    }
    surfaces[surface] = result;
  }
  const successful = wanted.filter((surface) => ['success', 'empty', 'partial'].includes(String((surfaces[surface] || {}).status)));
  const failed = wanted.filter((surface) => ['failed', 'partial', 'unsupported'].includes(String((surfaces[surface] || {}).status)));
  const bounded = wanted.some((surface) => (surfaces[surface] || {}).bounded || (surfaces[surface] || {}).warnings && (surfaces[surface] || {}).warnings.includes('bounded_limit_reached'));
  let status = 'failed';
  if (wanted.some((surface) => String((surfaces[surface] || {}).status) === 'partial')) status = 'partial';
  else if (failed.length && successful.length) status = 'partial';
  else if (failed.length) status = failed.length === wanted.length ? 'failed' : 'partial';
  else if (bounded) status = 'bounded';
  else if (successful.length === wanted.length) status = 'complete';
  if (!wanted.length) status = 'unsupported';
  return {
    ok: successful.length > 0,
    posts,
    surfaces,
    warnings: Array.from(new Set(warnings)),
    paging: Object.fromEntries(wanted.map((surface) => [surface, (surfaces[surface] || {}).paging || {}])),
    coverage: {
      status,
      complete: status === 'complete',
      bounded: status === 'bounded' || status === 'partial' || status === 'complete',
      historical_complete: false,
      requested_surfaces: wanted,
      successful_surfaces: successful,
      failed_surfaces: failed,
      limit_per_surface: requested,
      note: 'bounded profile feeds; historical completeness is not asserted'
    }
  };
})()
"""


if __name__ == "__main__":
    sys.exit(main())

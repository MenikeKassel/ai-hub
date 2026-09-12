#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知乎收藏夹全量归档：把收藏夹每一条内容的正文抓下来，机器可读 JSONL + 统计报告。

数据源：监控快照 latest.json（条目清单）→ 逐条抓正文：
  * answer  → 页面内 /api/v4/answers/{id}?include=content （HTML 正文）
  * article → 专栏页内 /api/articles/{id}（失败则导航该文章页解析 DOM）
  * pin     → 页面内 /api/v4/pins/{id}
  * zvideo  → 页面内 /api/v4/zvideos/{id}（元数据；视频本体不下载）

特性：断点续跑（items.jsonl 已完成的条目自动跳过）、限速、429 自适应退避、
失败清单与重试、进度写入 progress.json（供外部查看）。

用法：
  python zhihu_collection_archive.py --limit 6        # 小样本试跑
  python zhihu_collection_archive.py                  # 全量（可重复跑续传）
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import zhihu_local_capture as zlc  # noqa: E402
import zhihu_collection_monitor as zcm  # noqa: E402  （复用浏览器启停工具）

DEFAULT_LATEST = Path(r"D:\aiworkspace\ai-hub\_runtime\zhihu-collection-monitor\latest.json")
DEFAULT_OUT = Path(r"D:\aiworkspace\ai-hub\_runtime\zhihu-collection-monitor\archive")
PORT = 9222
COLLECTION_URL = "https://www.zhihu.com/collection/846575569"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def log(*parts: Any) -> None:
    print(datetime.now().strftime("%H:%M:%S"), *parts, flush=True)


_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+?(?:data-original|data-actualsrc|src)=\"([^\"]+)\"", re.I)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"</(div|h[1-6]|li|blockquote)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_images(html: str) -> list[str]:
    urls = []
    for match in _IMG_RE.finditer(html or ""):
        url = match.group(1).strip()
        if url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


class Session:
    """浏览器 + 两个页面上下文（www.zhihu.com / zhuanlan.zhihu.com）。"""

    def __init__(self, port: int, user_data_dir: str, profile: str, browser_path: str, no_launch: bool):
        self.port = port
        self.ep, self.launched = zcm.ensure_cdp(port, browser_path, user_data_dir, profile, no_launch, 45, False)
        self.www_tab = self._open_tab(COLLECTION_URL)
        self._nav(self.www_tab, COLLECTION_URL)
        self.zhuanlan_tab: dict[str, Any] | None = None

    # ---------- tab 管理 ----------
    def _open_tab(self, url: str) -> dict[str, Any]:
        for t in zlc.http_json(f"http://127.0.0.1:{self.port}/json/list", timeout=5):
            if t.get("webSocketDebuggerUrl") and str(t.get("url", "")).startswith(url[:55]):
                return t
        quoted = urllib.parse.quote(url, safe="")
        try:
            return zlc.http_json(f"http://127.0.0.1:{self.port}/json/new?{quoted}", timeout=8, method="PUT")
        except Exception:
            return zlc.http_json(f"http://127.0.0.1:{self.port}/json/new", timeout=8, method="PUT")

    def _nav(self, tab: dict[str, Any], url: str) -> None:
        with zlc.CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": url})
            zlc.wait_for_page(cdp, 30)
        time.sleep(1.5)

    def ensure_zhuanlan_tab(self, seed_url: str) -> dict[str, Any]:
        if self.zhuanlan_tab is None:
            self.zhuanlan_tab = self._open_tab(seed_url)
            self._nav(self.zhuanlan_tab, seed_url)
        return self.zhuanlan_tab

    def fetch(self, tab: dict[str, Any], expr: str, timeout: int = 60) -> str | None:
        """在指定 tab 上执行 fetch 表达式；带一次 tab 自愈重试。"""
        for attempt in range(3):
            try:
                with zlc.CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
                    cdp.call("Runtime.enable")
                    value = zlc.evaluate(cdp, expr, timeout=timeout, await_promise=True)
                return value if isinstance(value, str) else None
            except Exception:
                if attempt < 2:
                    time.sleep(3)
                    continue
        return None

    def close(self) -> None:
        for t in (self.www_tab, self.zhuanlan_tab):
            try:
                if t and t.get("id"):
                    urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/close/{t['id']}", timeout=5).read()
            except Exception:
                pass
        zcm.cleanup(self.port, self.launched, False, "")


# ---------- 各类型抓取 ----------

def js_fetch(expr_body: str) -> str:
    return f"(async()=>{{try{{{expr_body}}}catch(e){{return JSON.stringify({{status:0,error:String(e)}})}}}})()"


def fetch_answer(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    aid = item["id"]
    expr = js_fetch(
        f"const r=await fetch('/api/v4/answers/{aid}?include=content',{{credentials:'include'}});"
        "const j=await r.json();"
        "return JSON.stringify({status:r.status,id:j.id,type:j.type,url:j.url,"
        "content:j.content||'',created_time:j.created_time,updated_time:j.updated_time,"
        "voteup_count:j.voteup_count,comment_count:j.comment_count,"
        "content_need_truncated:j.content_need_truncated,is_collapsed:j.is_collapsed,"
        "question_title:((j.question||{}).title)||'',question_id:((j.question||{}).id)||'',"
        "author:((j.author||{}).name)||''})"
    )
    raw = session.fetch(session.www_tab, expr)
    return _finish(item, raw, method="answer-api")


def fetch_pin(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    pid = item["id"]
    expr = js_fetch(
        f"const r=await fetch('/api/v4/pins/{pid}',{{credentials:'include'}});"
        "const j=await r.json();const c=j.content;"
        "const cStr=(typeof c==='string')?c:JSON.stringify(c||'');"
        "return JSON.stringify({status:r.status,id:j.id,url:j.url,content:cStr,"
        "state:j.state,is_deleted:j.is_deleted,created:j.created,updated:j.updated,"
        "like_count:j.like_count,comment_count:j.comment_count,"
        "excerpt:j.excerpt||'',author:((j.author||{}).name)||''})"
    )
    raw = session.fetch(session.www_tab, expr)
    return _finish(item, raw, method="pin-api")


def fetch_zvideo(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    zid = item["id"]
    expr = js_fetch(
        f"const r=await fetch('/api/v4/zvideos/{zid}',{{credentials:'include'}});"
        "const j=await r.json();"
        "return JSON.stringify({status:r.status,id:j.id,title:j.title||'',"
        "description:j.description||'',url:j.url||'',created:j.created,updated:j.updated,"
        "voteup_count:j.voteup_count,comment_count:j.comment_count,"
        "author:((j.author||{}).name)||''})"
    )
    raw = session.fetch(session.www_tab, expr)
    return _finish(item, raw, method="zvideo-api")


def fetch_article(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    aid = item["id"]
    tab = session.ensure_zhuanlan_tab(item["url"])
    expr = js_fetch(
        f"const r=await fetch('/api/articles/{aid}',{{credentials:'include'}});"
        "const j=await r.json();"
        "return JSON.stringify({status:r.status,id:j.id,title:j.title||'',"
        "content:j.content||'',excerpt:j.excerpt||'',created:j.created,updated:j.updated,"
        "article_type:j.article_type||'',voteup_count:j.voteup_count,comment_count:j.comment_count,"
        "author:((j.author||{}).name)||''})"
    )
    raw = session.fetch(tab, expr)
    result = _finish(item, raw, method="article-api")
    if not result["ok"] or len(result.get("content_text") or "") < 40:
        # 兜底：直接在该文章页 DOM 里取正文
        dom_expr = (
            "(()=>{var b=document.querySelector('.RichText')||document.querySelector('.Post-RichText');"
            "return JSON.stringify({status:200,title:document.title.split(' - ')[0],"
            "content:b?b.innerHTML:'',author:''})})()"
        )
        session._nav(tab, item["url"])
        raw2 = session.fetch(tab, dom_expr)
        dom_result = _finish(item, raw2, method="article-dom")
        if dom_result["ok"] and len(dom_result.get("content_text") or "") > len(result.get("content_text") or ""):
            dom_result["fallback_reason"] = result.get("error") or "api content too short"
            return dom_result
    return result


def _finish(item: dict[str, Any], raw: str | None, *, method: str) -> dict[str, Any]:
    base = {
        "id": item["id"], "type": item["type"], "url": item["url"],
        "title": item.get("title", ""), "author": item.get("author", ""),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "method": method, "ok": False, "partial": False,
        "content_html": "", "content_text": "", "images": [], "error": "",
    }
    if raw is None:
        base["error"] = "cdp fetch failed"
        return base
    try:
        payload = json.loads(raw)
    except Exception as exc:
        base["error"] = f"bad json: {exc}"
        return base
    status = payload.get("status")
    if status != 200:
        base["error"] = f"status={status} {payload.get('error', '')}".strip()
        return base
    html = str(payload.get("content") or "")
    base["content_html"] = html
    base["content_text"] = html_to_text(html)
    base["images"] = extract_images(html)
    # 公共字段
    for key in ("created_time", "created", "updated_time", "updated", "voteup_count",
                "comment_count", "like_count", "question_title", "author", "article_type",
                "content_need_truncated", "is_collapsed", "is_deleted", "state", "description"):
        if payload.get(key) not in (None, "", False):
            base[key] = payload[key]
    if payload.get("title"):
        base["title"] = payload["title"]
    if payload.get("excerpt") and not base["content_text"]:
        base["content_text"] = str(payload["excerpt"])
    if payload.get("content_need_truncated") and status == 200:
        base["partial"] = True  # 长回答被折叠标记：正文可能被截断
    base["ok"] = True
    return base


# ---------- 主流程 ----------

def load_pending(latest_path: Path, out_dir: Path, only_types: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
    items = json.loads(latest_path.read_text(encoding="utf-8"))["items"]
    done: set[str] = set()
    jsonl = out_dir / "items.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("ok") or rec.get("partial"):
                    done.add(f"{rec['type']}:{rec['id']}")
            except Exception:
                continue
    pending = [i for i in items if i["key"] not in done]
    if only_types:
        pending = [i for i in pending if i["type"] in only_types]
    if limit:
        pending = pending[:limit]
    return pending


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="知乎收藏夹全量归档（断点续跑）。")
    parser.add_argument("--latest", default=str(DEFAULT_LATEST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--user-data-dir", default=zcm.DEFAULT_USER_DATA_DIR)
    parser.add_argument("--profile-directory", default=zcm.DEFAULT_PROFILE)
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--throttle", type=float, default=0.55, help="每条之间的基础间隔秒")
    parser.add_argument("--only-type", default="", help="只抓某类（answer/article/pin/zvideo）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "items.jsonl"
    only_types = [t for t in args.only_type.split(",") if t] or None
    pending = load_pending(Path(args.latest), out_dir, only_types, args.limit or None)
    total_all = len(json.loads(Path(args.latest).read_text(encoding="utf-8"))["items"])
    log(f"pending {len(pending)} / total {total_all}; out={out_dir}")
    if not pending:
        log("nothing to do (all fetched).")
        return 0

    browser_path = args.browser_path or zlc.default_browser_path("edge")
    session = Session(args.port, args.user_data_dir, args.profile_directory, browser_path, args.no_launch)
    throttled_sleep = args.throttle
    ok = fail = partial = 0
    done_count = 0
    failed: list[dict[str, str]] = []
    started = time.time()

    def write_progress() -> None:
        (out_dir / "progress.json").write_text(json.dumps({
            "total": len(pending), "done": done_count, "ok": ok, "fail": fail, "partial": partial,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    try:
        for index, item in enumerate(pending, 1):
            try:
                if item["type"] == "answer":
                    rec = fetch_answer(session, item)
                elif item["type"] == "article":
                    rec = fetch_article(session, item)
                elif item["type"] == "pin":
                    rec = fetch_pin(session, item)
                elif item["type"] == "zvideo":
                    rec = fetch_zvideo(session, item)
                else:
                    rec = _finish(item, None, method="skip")
                    rec["error"] = f"unsupported type {item['type']}"
            except Exception as exc:
                rec = _finish(item, None, method="exception")
                rec["error"] = f"{type(exc).__name__}: {exc}"

            with open(jsonl, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done_count += 1
            if rec.get("ok"):
                ok += 1
                if rec.get("partial"):
                    partial += 1
            else:
                fail += 1
                failed.append({"key": f"{item['type']}:{item['id']}", "error": str(rec.get("error", ""))[:200]})

            if index % 25 == 0 or index == len(pending):
                elapsed = time.time() - started
                rate = elapsed / index
                eta = rate * (len(pending) - index)
                log(f"PROGRESS {index}/{len(pending)} ok={ok} fail={fail} partial={partial} "
                    f"rate={rate:.2f}s/item eta={eta/60:.1f}min")
                write_progress()

            # 自适应退避：连续失败越多，间隔越大
            if len(failed) >= 5 and index == len(pending) - 1:
                pass
            if fail and index >= 10 and fail / index > 0.5:
                throttled_sleep = min(throttled_sleep * 1.6, 6.0)
            time.sleep(throttled_sleep)
    finally:
        session.close()
        write_progress()

    # 失败重试一遍
    if failed:
        log(f"retrying {len(failed)} failures ...")
        retry_pending = [i for i in pending if f"{i['type']}:{i['id']}" in {f['key'] for f in failed}]
        session = Session(args.port, args.user_data_dir, args.profile_directory, browser_path, args.no_launch)
        retry_ok = 0
        try:
            for item in retry_pending:
                if item["type"] == "answer":
                    rec = fetch_answer(session, item)
                elif item["type"] == "article":
                    rec = fetch_article(session, item)
                elif item["type"] == "pin":
                    rec = fetch_pin(session, item)
                else:
                    rec = fetch_zvideo(session, item)
                if rec.get("ok"):
                    rec["retried"] = True
                    with open(jsonl, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    retry_ok += 1
                    ok += 1
                    fail -= 1
                time.sleep(args.throttle)
        finally:
            session.close()
        log(f"retry done: recovered {retry_ok}/{len(retry_pending)}")

    report = {
        "started": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "total_all_items": total_all,
        "fetched_this_run": done_count,
        "ok": ok, "fail": fail, "partial": partial,
        "failed": failed,
        "jsonl": str(jsonl),
    }
    (out_dir / "_archive_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"DONE ok={ok} fail={fail} partial={partial} -> {out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

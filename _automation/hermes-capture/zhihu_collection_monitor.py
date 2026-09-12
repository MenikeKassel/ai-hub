#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知乎收藏夹变更监控（每日静默哨兵）。

监控用户知乎收藏夹（默认 846575569「我的收藏夹」）的条目变化：用专用隔离
Edge profile（zhihu-edge，已登录知乎）经 CDP 在页面内分页读取
``/api/v4/collections/<id>/items``，与上次快照比对，仅在"内容消失"时输出简报。

stdout 约定（供 Hermes cron ``no_agent`` 静默看门狗使用）：
  * 空 stdout            → 无变化（静默，不打扰）
  * 非空中文文本         → 变动简报 / 首次基准建立信息（会被投递）

退出码：
  0  成功（无变化 / 有变化 / 首次建立基准）
  2  运行错误（浏览器、CDP、网络、分页失败等）
  3  登录态失效（需在 zhihu-edge 浏览器里重新登录知乎）
  4  扫描疑似不完整（未覆盖上次基准，需人工确认；如确为本人大量清理可重置基准）

手工运行与调试：
  python zhihu_collection_monitor.py
  python zhihu_collection_monitor.py --json --debug
  python zhihu_collection_monitor.py --state-dir <临时目录>   # 演练，不动正式状态
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import zhihu_local_capture as zlc  # noqa: E402  （同目录，纯 stdlib CDP 工具）

COLLECTION_ID = "846575569"
COLLECTION_NAME = "我的收藏夹"
COLLECTION_URL = f"https://www.zhihu.com/collection/{COLLECTION_ID}"

DEFAULT_STATE_DIR = Path(r"D:\aiworkspace\ai-hub\_runtime\zhihu-collection-monitor")
DEFAULT_PORT = 9222
DEFAULT_USER_DATA_DIR = r"C:\Users\12973\AppData\Local\hermes\browser-profiles\zhihu-edge"
DEFAULT_PROFILE = "Default"

PAGE_LIMIT = 20        # 收藏夹接口实测安全页大小（limit=100 返回空）
MAX_PAGES = 120        # 安全上限：1079 条约需 54 页
PARTIAL_RATIO = 0.90   # 相对上次快照骤降阈值（疑似不完整，暂不覆盖基准）
PARTIAL_TOTALS_RATIO = 0.70  # totals 是含已失效条目的原始计数，允许可见条目低于 totals
HISTORY_KEEP = 60      # 历史快照保留份数

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_LOGIN = 3
EXIT_PARTIAL = 4


class MonitorError(Exception):
    """带退出码的运行错误。"""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def dbg(enabled: bool, *parts: Any) -> None:
    if enabled:
        print("[debug]", *parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 浏览器管理

def launch_browser(browser_path: str, user_data_dir: str, profile: str, port: int) -> None:
    """离屏启动专用 Edge（不弹到用户面前），等 CDP 由调用方轮询。"""
    cmd = [
        browser_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-position=-32000,-32000",
        "--window-size=1366,900",
        "--new-window",
        "about:blank",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_cdp(port: int, browser_path: str, user_data_dir: str, profile: str,
               no_launch: bool, wait_seconds: int, debug: bool) -> tuple[dict[str, Any], bool]:
    """返回 (CDP version 端点, 是否由我们启动)。复用已有实例时不负责收尾它。"""
    ep = zlc.wait_for_cdp(port, 2)
    if ep:
        dbg(debug, f"reuse existing CDP on {port}")
        return ep, False
    if no_launch:
        raise MonitorError(
            f"浏览器 DevTools（127.0.0.1:{port}）未就绪，且指定了 --no-launch。"
        )
    dbg(debug, f"launching edge offscreen: {browser_path}")
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    launch_browser(browser_path, user_data_dir, profile, port)
    ep = zlc.wait_for_cdp(port, wait_seconds)
    if not ep:
        raise MonitorError(f"启动了浏览器但 {wait_seconds}s 内 DevTools 未就绪（127.0.0.1:{port}）。")
    return ep, True


def find_or_create_tab(port: int) -> tuple[dict[str, Any], bool]:
    """找到既有收藏夹标签页，或新建一个。返回 (tab, 是否我们新建)。"""
    try:
        tabs = zlc.http_json(f"http://127.0.0.1:{port}/json/list", timeout=5)
    except Exception as exc:
        raise MonitorError(f"读取浏览器标签页列表失败：{exc}") from exc
    for tab in tabs:
        if f"collection/{COLLECTION_ID}" in str(tab.get("url", "")) and tab.get("webSocketDebuggerUrl"):
            return tab, False
    quoted = urllib.parse.quote(COLLECTION_URL, safe="")
    try:
        tab = zlc.http_json(f"http://127.0.0.1:{port}/json/new?{quoted}", timeout=5, method="PUT")
    except urllib.error.HTTPError:
        tab = zlc.http_json(f"http://127.0.0.1:{port}/json/new", timeout=5, method="PUT")
    if not isinstance(tab, dict) or not tab.get("webSocketDebuggerUrl"):
        raise MonitorError("无法创建收藏夹标签页。")
    return tab, True


def cleanup(port: int, launched: bool, created_tab: bool, tab_id: str) -> None:
    """关掉我们开的标签页；若浏览器由我们启动，再整体关闭（仅限本专用实例）。"""
    if created_tab and tab_id:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=5).read()
        except Exception:
            pass
    if launched:
        try:
            ep = zlc.http_json(f"http://127.0.0.1:{port}/json/version", timeout=3)
            ws = ep.get("webSocketDebuggerUrl")
            if ws:
                with zlc.CdpClient(ws) as browser_cdp:
                    browser_cdp.call("Browser.close", timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------- 数据读取

def fetch_page(cdp: "zlc.CdpClient", offset: int, retries: int = 2, debug: bool = False) -> dict[str, Any]:
    """页面内分页读取一页条目（字段裁剪在 JS 侧完成，减少 CDP 传输量）。"""
    url = f"/api/v4/collections/{COLLECTION_ID}/items?offset={offset}&limit={PAGE_LIMIT}"
    expression = (
        "(async()=>{try{"
        f"const r=await fetch('{url}',{{credentials:'include'}});"
        "const t=await r.text();"
        "let j=null;try{j=JSON.parse(t)}catch(e){}"
        "if(!j){return JSON.stringify({status:r.status,parse_error:true,head:t.slice(0,200)})}"
        "const items=(j.data||[]).map(x=>{const c=x.content||{};return {"
        "type:c.type,id:String(c.id||''),url:c.url||'',"
        "title:c.title||((c.question||{}).title)||'',"
        "author:((c.author||{}).name)||'',"
        "is_deleted:!!c.is_deleted}});"
        "return JSON.stringify({status:r.status,totals:(j.paging||{}).totals,"
        "is_end:!!(j.paging||{}).is_end,items:items})"
        "}catch(e){return JSON.stringify({status:0,error:String(e)})}})()"
    )
    last_error = "unknown"
    for attempt in range(retries + 1):
        try:
            raw = zlc.evaluate(cdp, expression, timeout=90, await_promise=True)
            payload = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(payload, dict):
                last_error = "unparsable response"
            elif payload.get("parse_error"):
                last_error = f"non-JSON body: {payload.get('head', '')[:120]}"
            elif payload.get("status") == 200:
                return payload
            else:
                last_error = f"status={payload.get('status')} {payload.get('error', '')}".strip()
        except Exception as exc:  # CDP 超时等
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            dbg(debug, f"page offset={offset} retry {attempt + 1}: {last_error}")
            time.sleep(2 + 2 * attempt)
    raise MonitorError(f"读取收藏夹第 {offset // PAGE_LIMIT + 1} 页失败：{last_error}")


def collect_items(cdp: "zlc.CdpClient", max_pages: int, debug: bool = False) -> dict[str, Any]:
    """分页拉取全部条目，返回 {items, totals, pages, is_end}。"""
    items: list[dict[str, Any]] = []
    totals: int | None = None
    is_end = False
    pages = 0
    offset = 0
    while pages < max_pages:
        payload = fetch_page(cdp, offset, debug=debug)
        totals = payload.get("totals") or totals
        batch = payload.get("items") or []
        items.extend(normalize_item(raw) for raw in batch)
        is_end = bool(payload.get("is_end"))
        pages += 1
        dbg(debug, f"page {pages}: +{len(batch)} (total {len(items)}/{totals}, is_end={is_end})")
        if is_end or not batch:
            break
        if totals and len(items) >= totals:
            break
        offset += PAGE_LIMIT
        time.sleep(0.4)
    return {"items": items, "totals": totals, "pages": pages, "is_end": is_end}


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """把页面裁剪后的条目规整为快照条目。"""
    item_type = str(raw.get("type") or "unknown").strip() or "unknown"
    item_id = str(raw.get("id") or "").strip()
    return {
        "key": f"{item_type}:{item_id}",
        "type": item_type,
        "id": item_id,
        "title": str(raw.get("title") or "").strip(),
        "author": str(raw.get("author") or "").strip(),
        "url": str(raw.get("url") or "").strip(),
        "is_deleted": bool(raw.get("is_deleted")),
    }


def check_login_state(cdp: "zlc.CdpClient") -> None:
    """快速登录态检查：页面里出现「请登录」且没有内容计数 → 判定登录失效。"""
    try:
        body = zlc.evaluate(
            cdp,
            "(()=>{const t=(document.body&&document.body.innerText)||'';"
            "return JSON.stringify({need_login:t.indexOf('请登录')>-1,"
            "has_count:t.indexOf('个内容')>-1})})()",
            timeout=20,
        )
        state = json.loads(body) if isinstance(body, str) else {}
    except Exception:
        return  # 检查失败不阻塞，后续 API 会给出真实信号
    if state.get("need_login") and not state.get("has_count"):
        raise MonitorError(
            "收藏夹页显示「请登录」——zhihu-edge 浏览器登录态可能失效，"
            "需重新登录后再让监控继续。",
            code=EXIT_LOGIN,
        )


def classify_liveness(cdp: "zlc.CdpClient", item: dict[str, Any], debug: bool = False) -> str | None:
    """对消失的回答做存活探测：200=仍可访问, 404=无法访问；其它情况不下结论。

    仅 /api/v4/answers/<id> 在页面内可用（文章接口 403，不做判定）。
    """
    if item.get("type") != "answer" or not item.get("id"):
        return None
    expression = (
        "(async()=>{try{"
        f"const r=await fetch('/api/v4/answers/{item['id']}',{{credentials:'include'}});"
        "return String(r.status)}catch(e){return 'ERR'}})()"
    )
    try:
        status = str(zlc.evaluate(cdp, expression, timeout=20, await_promise=True) or "")
    except Exception:
        return None
    dbg(debug, f"liveness {item.get('id')}: {status}")
    if status == "200":
        return "内容仍可访问（可能只是被移出收藏）"
    if status == "404":
        return "内容已无法访问（疑似被删除或下架）"
    return None


# ---------------------------------------------------------------- 快照与比对

def load_previous(state_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """读取上次快照；损坏时隔离旧文件并返回 ``(None, 说明)``，由调用方按首次基准处理。"""
    latest = state_dir / "latest.json"
    if not latest.exists():
        return None, ""
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("bad shape")
        return payload, ""
    except Exception:
        broken = state_dir / f"latest.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        try:
            latest.replace(broken)
        except Exception:
            pass
        return None, f"上次快照文件损坏（已隔离为 {broken.name}）"


def snapshot_payload(items: list[dict[str, Any]], totals: int | None, pages: int) -> dict[str, Any]:
    return {
        "collection": COLLECTION_ID,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "totals": totals,
        "count": len(items),
        "pages": pages,
        "items": items,
    }


def save_snapshot(state_dir: Path, payload: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    latest = state_dir / "latest.json"
    tmp = state_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, latest)
    history = state_dir / "history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history / f"{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    prune_history(history)


def prune_history(history_dir: Path, keep: int = HISTORY_KEEP) -> None:
    """保留最近 ``keep`` 份历史快照，其余删除。"""
    backups = sorted(history_dir.glob("*.json"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def append_log(state_dir: Path, line: str) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "monitor.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def scan_sanity_error(totals: int | None, count: int, is_end: bool,
                      prev_count: int | None, max_pages: int) -> str | None:
    """返回错误文案（疑似不完整），否则 None。宁可误报'不完整'，不误报'大删除'。"""
    if not is_end:
        return f"分页未到末尾（取到 {count} 条，接口报告 {totals} 条），可能被限流。"
    if totals and count < totals * PARTIAL_TOTALS_RATIO:
        return f"仅取到 {count} 条，明显少于接口报告的 {totals} 条。"
    if prev_count and count < prev_count * PARTIAL_RATIO:
        return (f"本次 {count} 条，比上次的 {prev_count} 条骤降超过 10%，疑似扫描不完整，"
                f"暂不更新基准（如确为你本人大量清理，请联系 Hermes 重置基准）。")
    return None


def diff_items(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    prev_map = {it["key"]: it for it in previous}
    cur_map = {it["key"]: it for it in current}
    removed = [prev_map[k] for k in prev_map if k not in cur_map]
    added = [cur_map[k] for k in cur_map if k not in prev_map]
    newly_deleted = [
        cur_map[k] for k in cur_map
        if cur_map[k].get("is_deleted") and k in prev_map and not prev_map[k].get("is_deleted")
    ]
    return {"removed": removed, "added": added, "newly_deleted": newly_deleted, "changed": True}


_TYPE_CN = {
    "answer": "回答",
    "article": "文章",
    "pin": "想法",
    "video": "视频",
    "zvideo": "视频",
}


def type_cn(item_type: str) -> str:
    return _TYPE_CN.get(item_type, item_type or "内容")


def build_baseline_message(count: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"✅ 知乎收藏夹监控已建立基准：{COLLECTION_NAME}共 {count} 条内容（{now}）。\n"
            f"之后仅在内容消失时提醒你。")


def build_change_report(diff: dict[str, Any], prev_count: int, cur_count: int,
                        cdp: "zlc.CdpClient | None", debug: bool = False) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    removed = diff["removed"]
    deleted = diff["newly_deleted"]
    added = diff["added"]
    if not removed and not deleted:
        return ""
    lines = [f"📌 知乎收藏夹「{COLLECTION_NAME}」有内容消失（{now}）",
             f"共 {prev_count} → {cur_count} 条", ""]
    if removed:
        lines.append(f"❗已从收藏夹消失 {len(removed)} 条：")
        for item in removed:
            title = item.get("title") or "（无标题）"
            author = item.get("author") or "未知作者"
            lines.append(f"• 《{title}》— {author}（{type_cn(item.get('type', ''))}）")
            if item.get("url"):
                lines.append(f"  {item['url']}")
            live = classify_liveness(cdp, item, debug=debug) if cdp is not None else None
            if live:
                lines.append(f"  → {live}")
        lines.append("")
    if deleted:
        lines.append(f"❗被知乎标记为已删除 {len(deleted)} 条：")
        for item in deleted:
            title = item.get("title") or "（无标题）"
            author = item.get("author") or "未知作者"
            lines.append(f"• 《{title}》— {author}（{type_cn(item.get('type', ''))}）")
            if item.get("url"):
                lines.append(f"  {item['url']}")
        lines.append("")
    if added:
        lines.append(f"（另：本次新增 {len(added)} 条，详情见快照。）")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------- 主流程

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知乎收藏夹变更监控（静默哨兵模式）。")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                        help="状态目录（latest.json / history / monitor.log）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="浏览器 CDP 端口")
    parser.add_argument("--user-data-dir", default=DEFAULT_USER_DATA_DIR,
                        help="专用浏览器 user-data-dir")
    parser.add_argument("--profile-directory", default=DEFAULT_PROFILE, help="浏览器 profile 目录名")
    parser.add_argument("--browser-path", default="", help="浏览器可执行文件路径（默认自动探测 Edge）")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="分页安全上限")
    parser.add_argument("--no-launch", action="store_true", help="不自动启动浏览器（只连已有 CDP）")
    parser.add_argument("--keep-browser", action="store_true",
                        help="脚本结束时保留自己启动的浏览器（默认关闭）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果（调试用）")
    parser.add_argument("--debug", action="store_true", help="输出调试信息到 stderr")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    state_dir = Path(args.state_dir).expanduser()
    browser_path = args.browser_path or zlc.default_browser_path("edge")
    if not Path(browser_path).exists():
        raise MonitorError(f"找不到浏览器可执行文件：{browser_path}")

    launched = False
    created_tab = False
    tab_id = ""
    result: dict[str, Any]

    ep, launched = ensure_cdp(args.port, browser_path, args.user_data_dir,
                              args.profile_directory, args.no_launch, 45, args.debug)
    try:
        tab, created_tab = find_or_create_tab(args.port)
        tab_id = str(tab.get("id") or "")
        with zlc.CdpClient(tab["webSocketDebuggerUrl"]) as cdp:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": COLLECTION_URL})
            zlc.wait_for_page(cdp, 30)
            time.sleep(3)
            check_login_state(cdp)

            collected = collect_items(cdp, args.max_pages, debug=args.debug)
            items, totals = collected["items"], collected["totals"]
            if not items:
                raise MonitorError(
                    "未取到任何条目：登录态可能失效或被知乎拦截（可手动打开收藏夹确认）。",
                    code=EXIT_LOGIN,
                )

            previous, corrupt_note = load_previous(state_dir)
            prev_items = (previous or {}).get("items") or []
            prev_count = len(prev_items) if previous else None

            sanity = scan_sanity_error(totals, len(items), collected["is_end"], prev_count, args.max_pages)
            payload = snapshot_payload(items, totals, collected["pages"])
            diff = diff_items(prev_items, items) if previous else {"removed": [], "added": [], "newly_deleted": []}

            if sanity:
                # 疑似不完整：不覆盖基准，报错退出（cron 会把失败转成告警）
                raise MonitorError(f"扫描疑似不完整：{sanity}", code=EXIT_PARTIAL)

            report_text = ""
            mode = "nochange"
            if previous is None:
                mode = "baseline"
                report_text = build_baseline_message(len(items))
                if corrupt_note:
                    report_text += f"\n（注：{corrupt_note}）"
            elif diff["removed"] or diff["newly_deleted"]:
                mode = "changes"
                report_text = build_change_report(diff, prev_count, len(items), cdp, debug=args.debug)

            save_snapshot(state_dir, payload)
            append_log(state_dir, "\t".join([
                datetime.now().isoformat(timespec="seconds"),
                f"count={len(items)}",
                f"totals={totals}",
                f"pages={collected['pages']}",
                f"removed={len(diff['removed'])}",
                f"added={len(diff['added'])}",
                f"newly_deleted={len(diff['newly_deleted'])}",
                f"mode={mode}",
            ]))
            result = {
                "ok": True,
                "mode": mode,
                "count": len(items),
                "totals": totals,
                "pages": collected["pages"],
                "removed": diff["removed"],
                "added_count": len(diff["added"]),
                "newly_deleted": diff["newly_deleted"],
                "report": report_text,
                "state_dir": str(state_dir),
            }
    finally:
        if not args.keep_browser:
            cleanup(args.port, launched, created_tab, tab_id)
        elif created_tab and tab_id:
            # 保留浏览器时仍收起我们打开的标签页
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/close/{tab_id}", timeout=5).read()
            except Exception:
                pass
    return result


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        result = run(args)
    except MonitorError as exc:
        message = f"⚠️ 知乎收藏夹监控异常：{exc}"
        print(message, flush=True)
        return exc.code
    except KeyboardInterrupt:
        print("⚠️ 知乎收藏夹监控被中断。", flush=True)
        return EXIT_ERROR
    except Exception as exc:
        print(f"⚠️ 知乎收藏夹监控异常：{type(exc).__name__}: {exc}", flush=True)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    elif result["report"]:
        print(result["report"], flush=True)
    # report 为空 → 静默（无变化）
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

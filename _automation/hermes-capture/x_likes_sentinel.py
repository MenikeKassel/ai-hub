#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""X 收藏夹哨兵（每日静默防丢监控）。

检查档案（likes_full.jsonl）中的推文在 X 上是否仍然可访问，发现不可访问时输出
简报。全程免登录：只使用公开嵌入接口（cdn.syndication.twimg.com / publish.x.com），
不进行任何账号操作；不保存、不展示推文正文（正文节选只取自本机档案）。

stdout 约定（供 Hermes cron no_agent 静默看门狗使用）：
  * 空 stdout    → 无新增失效（静默，不打扰）
  * 非空中文文本 → 上线基准信息 / 新失效简报（会被投递）

退出码：
  0  成功（含静默、基准、简报）
  2  运行错误（档案不可读、状态损坏等）
  3  网络/接口整体不可用（如本地代理未开启；交由 cron 失败告警）

用法：
  python x_likes_sentinel.py                                     # 正式运行（默认每日 600 条轮转）
  python x_likes_sentinel.py --limit 30 --state-dir <临时目录>    # 演练（不动正式状态）
  python x_likes_sentinel.py --json                              # 调试输出
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ARCHIVE_DEFAULT = Path(r"D:\aiworkspace\ai-hub\_runtime\x-collection-archive\archive\likes_full.jsonl")
STATE_DEFAULT = Path(r"D:\aiworkspace\x-collection-archive\state")
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=en&token=hermes"
OEMBED = "https://publish.x.com/oembed?url=https%3A%2F%2Ftwitter.com%2Fi%2Fstatus%2F{id}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_LIMIT = 600
REQUEST_TIMEOUT = 15
BASE_DELAY = 0.30
JITTER = 0.25
HISTORY_KEEP = 60

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NETWORK = 3


class SentinelError(Exception):
    """带退出码的运行错误。"""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def single_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def fetch(opener: urllib.request.OpenerDirector, url: str) -> tuple[int | None, bytes, str]:
    """返回 (HTTP 状态或 None, 响应体, 传输错误信息)。"""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(2_000_000), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:
            body = b""
        return exc.code, body, ""
    except Exception as exc:  # URLError / timeout / SSL ...
        return None, b"", f"{type(exc).__name__}: {exc}"


def classify_syndication(status: int | None, body: bytes) -> str:
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return "unknown"
        return "alive" if isinstance(data, dict) and data else "unknown"
    if status in (404, 410):
        return "gone_suspect"
    if status == 429:
        return "throttled"
    return "unknown"


def confirm_gone(opener: urllib.request.OpenerDirector, tid: str) -> str:
    """oembed 复核：'gone' / 'alive' / 'unresolved'。"""
    status, body, _err = fetch(opener, OEMBED.format(id=tid))
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return "unresolved"
        return "alive" if isinstance(data, dict) and data else "unresolved"
    if status in (404, 410):
        return "gone"
    return "unresolved"


def load_archive(path: Path, skip_ids: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            tid = str(record.get("id") or "")
            if not re.fullmatch(r"[0-9]+", tid) or tid in skip_ids:
                continue
            created = record.get("created_at") or ""
            entries.append({
                "id": tid,
                "date": created[:10] if len(created) >= 10 else "日期未知",
                "month": created[:7] if len(created) >= 7 else "0000-00",
                "user": single_line(record.get("screen_name")) or "未知",
                "excerpt": single_line(record.get("full_text"))[:60],
            })
    entries.sort(key=lambda e: (e["date"], int(e["id"])), reverse=True)
    return entries


def load_skip_ids(state_dir: Path) -> set[str]:
    path = state_dir / "skip_ids.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("skip_ids", [])}
    except Exception:
        return set()


def load_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "state.json"
    if not path.exists():
        return {"cursor": 0, "checked_total": 0, "runs": 0, "first_run_done": False,
                "gone": {}, "retry": {}, "last_run": None, "last_mode": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
    except Exception as exc:
        raise SentinelError(f"状态文件损坏（{path}）：{exc}") from exc
    data.setdefault("cursor", 0)
    data.setdefault("checked_total", 0)
    data.setdefault("runs", 0)
    data.setdefault("first_run_done", False)
    data.setdefault("gone", {})
    data.setdefault("retry", {})
    return data


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, state_dir / "state.json")


def append_log(state_dir: Path, line: str) -> None:
    try:
        with open(state_dir / "sentinel.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def write_history(state_dir: Path, payload: dict[str, Any]) -> None:
    history = state_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (history / f"{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    backups = sorted(history.glob("*.json"))
    for old in backups[:-HISTORY_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def gone_block(items: list[dict[str, Any]], total_gone: int) -> list[str]:
    lines = [f"❗本次新发现已不可访问 {len(items)} 条（累计 {total_gone} 条）："]
    for item in items:
        meta = item.get("meta") or {}
        excerpt = meta.get("excerpt") or "（无正文节选）"
        lines.append(f"• {meta.get('date') or '?'} @{meta.get('user') or '?'}：「{excerpt}」")
        lines.append(f"  https://x.com/i/status/{item['id']}（正文存于档案 全文/{meta.get('month') or '?'}.md）")
    return lines


def build_baseline(stats: dict[str, int], new_gone: list[dict[str, Any]], state: dict[str, Any],
                   total: int, limit: int, span: str) -> str:
    lines = ["✅ X收藏夹哨兵已上线（每日轮转 · 免登录检查原帖可访问性）",
             f"首查 {stats['checked']} 条：正常 {stats['alive']} 条，已不可访问 {stats['gone_new']} 条，待复核 {stats['unknown']} 条。",
             f"档案共 {total} 条（{span}）；每日抽查约 {limit} 条，全体一轮约 {math.ceil(total / max(limit, 1))} 天。",
             "此后仅在发现内容在 X 上不可访问（被删除/账号异常）时提醒；档案正文不受影响，无需处理。"]
    if new_gone:
        lines.append("")
        lines.extend(gone_block(new_gone, len(state["gone"])))
    return "\n".join(lines)


def build_changes(new_gone: list[dict[str, Any]], stats: dict[str, int],
                  state: dict[str, Any], total: int) -> str:
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📌 X收藏夹有内容在 X 上不可访问（{when}）",
             f"档案共 {total} 条 · 累计发现不可访问 {len(state['gone'])} 条（本次新增 {len(new_gone)} 条）", ""]
    lines.extend(gone_block(new_gone, len(state["gone"])))
    lines.append("")
    lines.append(f"（本次检查 {stats['checked']} 条 · 轮转进度 {state['cursor']}/{total}）")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    state_dir = Path(args.state_dir)
    archive = Path(args.archive)
    if not archive.exists():
        raise SentinelError(f"档案不存在：{archive}")
    state_dir.mkdir(parents=True, exist_ok=True)
    skip_ids = load_skip_ids(state_dir)
    entries = load_archive(archive, skip_ids)
    total = len(entries)
    if total == 0:
        raise SentinelError("档案中没有可检查的推文 ID")
    state = load_state(state_dir)
    opener = urllib.request.build_opener()  # 自动使用环境代理（HTTPS_PROXY）

    run_list: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for tid, meta in list(state["retry"].items()):
        if tid not in seen and tid not in state["gone"]:
            run_list.append((tid, meta))
            seen.add(tid)
    cursor = int(state["cursor"]) % total
    slots = 0
    while slots < args.limit:
        entry = entries[cursor]
        cursor = (cursor + 1) % total
        slots += 1
        if entry["id"] in seen or entry["id"] in state["gone"]:
            continue
        seen.add(entry["id"])
        run_list.append((entry["id"], entry))

    stats = {"checked": 0, "alive": 0, "gone_new": 0, "flip": 0, "unknown": 0,
             "throttled": 0, "network_errors": 0}
    new_gone: list[dict[str, Any]] = []
    unresolved: dict[str, Any] = {}
    throttled = False

    for position, (tid, meta) in enumerate(run_list, 1):
        status, body, err = fetch(opener, SYNDICATION.format(id=tid))
        if err:
            stats["network_errors"] += 1
        verdict = classify_syndication(status, body)
        if verdict == "alive":
            stats["alive"] += 1
            state["retry"].pop(tid, None)
        elif verdict == "gone_suspect":
            time.sleep(0.2 + random.random() * 0.2)
            confidence = confirm_gone(opener, tid)
            if confidence == "gone":
                stats["gone_new"] += 1
                if tid not in state["gone"]:
                    state["gone"][tid] = {"detected": now_iso(), "meta": meta}
                    new_gone.append({"id": tid, "meta": meta})
                state["retry"].pop(tid, None)
            elif confidence == "alive":
                stats["flip"] += 1
                state["retry"].pop(tid, None)
            else:
                stats["unknown"] += 1
                unresolved[tid] = meta
        elif verdict == "throttled":
            stats["throttled"] = 1
            throttled = True
            break
        else:
            stats["unknown"] += 1
            unresolved[tid] = meta
        stats["checked"] += 1
        if position % 25 == 0:
            print(f"[sentinel] progress {stats['checked']}/{len(run_list)}", file=sys.stderr, flush=True)
        time.sleep(BASE_DELAY + random.random() * JITTER)

    state["cursor"] = cursor
    state["checked_total"] = int(state["checked_total"]) + stats["checked"]
    state["runs"] = int(state["runs"]) + 1
    state["retry"] = unresolved
    state["last_run"] = now_iso()

    mode = "none"
    report = ""
    if not state["first_run_done"]:
        mode = "baseline"
        span = f"{entries[-1]['date']} ~ {entries[0]['date']}"
        report = build_baseline(stats, new_gone, state, total, args.limit, span)
        state["first_run_done"] = True
    elif new_gone:
        mode = "changes"
        report = build_changes(new_gone, stats, state, total)

    state["last_mode"] = mode
    save_state(state_dir, state)
    append_log(state_dir, "\t".join([
        now_iso(), f"mode={mode}", f"checked={stats['checked']}",
        f"alive={stats['alive']}", f"gone_new={stats['gone_new']}",
        f"flip={stats['flip']}", f"unknown={stats['unknown']}",
        f"throttled={stats['throttled']}", f"gone_total={len(state['gone'])}",
        f"cursor={state['cursor']}/{total}",
    ]))
    write_history(state_dir, {
        "ts": now_iso(), "mode": mode, "checked": stats["checked"], "total": total,
        "alive": stats["alive"], "gone_new": stats["gone_new"], "flip": stats["flip"],
        "unknown": stats["unknown"], "throttled": bool(stats["throttled"]),
        "gone_total": len(state["gone"]), "new_gone_ids": [g["id"] for g in new_gone],
        "cursor": state["cursor"], "limit": args.limit,
    })

    result = {"ok": True, "mode": mode, "checked": stats["checked"], "total": total,
              "cursor": state["cursor"], "gone_total": len(state["gone"]),
              "new_gone": new_gone, "report": report, "state_dir": str(state_dir)}

    if stats["checked"] == 0 and stats["network_errors"] > 0:
        print("⚠️ X收藏夹哨兵：网络/接口不可用（请检查本机代理是否开启）。本次未完成任何检查。", flush=True)
        return {**result, "ok": False, "exit": EXIT_NETWORK}
    if throttled and stats["checked"] < 20:
        print("⚠️ X收藏夹哨兵：接口限流，本轮提前结束（若持续出现请留意）。", flush=True)
        return {**result, "ok": False, "exit": EXIT_NETWORK}
    result["exit"] = EXIT_OK
    return result


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="X 收藏夹哨兵（静默防丢监控）。")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="本次检查条数（轮转）")
    parser.add_argument("--state-dir", default=str(STATE_DEFAULT), help="状态目录")
    parser.add_argument("--archive", default=str(ARCHIVE_DEFAULT), help="主档 JSONL 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果（调试）")
    args = parser.parse_args()
    try:
        result = run(args)
    except SentinelError as exc:
        print(f"⚠️ X收藏夹哨兵异常：{exc}", flush=True)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("⚠️ X收藏夹哨兵被中断。", flush=True)
        return EXIT_ERROR
    except Exception as exc:
        print(f"⚠️ X收藏夹哨兵异常：{type(exc).__name__}: {exc}", flush=True)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    elif result["report"]:
        print(result["report"], flush=True)
    # report 为空 → 静默（无新增失效）
    return int(result.get("exit", EXIT_OK))


if __name__ == "__main__":
    raise SystemExit(main())

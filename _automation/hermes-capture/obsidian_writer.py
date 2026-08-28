from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any


INVALID_FILENAME_CHARS = r'<>:"/\\|?*'


def path_is_in_output_dir(relative_path: str, output_dir: str) -> bool:
    relative_parts = Path(str(relative_path).replace("\\", "/")).parts
    output_parts = Path(str(output_dir).replace("\\", "/").strip("/")).parts
    if not relative_parts or not output_parts or ".." in relative_parts or ".." in output_parts:
        return False
    return relative_parts[: len(output_parts)] == output_parts


def ensure_vault_dirs(vault_path: str) -> None:
    for name in [
        "00_Inbox",
        "01_Sources",
        "02_Concepts",
        "03_Entities",
        "04_Projects",
        "05_Strategies",
        "06_Logs",
        "90_Archive",
    ]:
        Path(vault_path, name).mkdir(parents=True, exist_ok=True)


def write_inbox_note(item: dict[str, Any], notion_url: str, config: dict[str, Any]) -> str:
    vault = Path(config["vault_path"]).resolve()
    inbox_dir = item.get("obsidian_output_dir") or config.get("obsidian_inbox_dir", "00_Inbox")
    ensure_vault_dirs(str(vault))
    target_dir = (vault / str(inbox_dir)).resolve()
    if not target_dir.is_relative_to(vault):
        raise RuntimeError(f"Refusing to write outside vault: {inbox_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    date_prefix = datetime.now().strftime("%Y-%m-%d")
    slug = slugify(item.get("title") or item.get("url") or "idea")
    filename = f"{date_prefix}__{slug}.md"
    path = unique_path(target_dir / filename)
    rel_path = path.relative_to(vault).as_posix()

    path.write_text(build_markdown(item, notion_url), encoding="utf-8")
    return rel_path


def overwrite_note(relative_path: str, item: dict[str, Any], notion_url: str, config: dict[str, Any]) -> str:
    vault = Path(config["vault_path"]).resolve()
    path = (vault / relative_path).resolve()
    if not path.is_relative_to(vault):
        raise RuntimeError(f"Refusing to write outside vault: {relative_path}")

    ensure_vault_dirs(str(vault))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_markdown(item, notion_url), encoding="utf-8")
    return path.relative_to(vault).as_posix()


def build_markdown(item: dict[str, Any], notion_url: str) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    title = item.get("title") or "Untitled capture"
    content = trim_block(item.get("content") or "", 8000)
    local_media = list(item.get("local_media") or [])
    note = item.get("note") or ""
    summary = item.get("summary") or "待整理。"

    frontmatter = {
        "type": "source",
        "status": item.get("status", "Inbox"),
        "source_url": item.get("url") or "",
        "source_type": item.get("source_type", "网页"),
        "project": item.get("project", "知识管理"),
        "notion_url": notion_url,
        "captured_at": now,
        "value_score": item.get("value_score", 2),
        "fetch_method": item.get("fetch_method", ""),
        "fetch_status": "ok" if item.get("fetch_ok", True) else "failed",
        "object_hint": item.get("object_hint", "来源"),
        "heavy_backend": item.get("heavy_backend", ""),
        "heavy_backend_status": item.get("heavy_backend_status", ""),
        "heavy_backend_job_id": item.get("heavy_backend_job_id", ""),
    }

    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {yaml_value(value)}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## 抓取状态",
            f"- 方法: {item.get('fetch_method') or 'unknown'}",
            f"- 状态: {'成功' if item.get('fetch_ok', True) else '失败'}",
            f"- 错误: {item.get('fetch_error')}" if item.get("fetch_error") else "- 错误: 无",
            f"- 补采: {item.get('heavy_backend')} / {item.get('heavy_backend_status')}" if item.get("heavy_backend_status") else "- 补采: 未入队",
            f"- 补采任务: {item.get('heavy_backend_job_id')}" if item.get("heavy_backend_job_id") else "- 补采任务: 无",
            "",
            "## 对象处理建议",
            object_guidance(item),
            "",
            "## 摘要",
            summary,
            "",
            "## 我的备注",
            note or "无。",
            "",
            "## 原始内容/快照",
            content or "未能读取正文，先保留来源链接。",
            "",
            "## 媒体快照",
            *(f"![[{path}]]" for path in local_media),
            "无本地媒体。" if not local_media else "",
            "",
            "## 下一步",
            "- [ ] 判断是否需要提炼到 Concepts / Projects / Strategies",
            "- [ ] 如果涉及股票、KOL、公司或行业，补充实体链接",
            "",
        ]
    )
    return "\n".join(lines)


def yaml_value(value: Any) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def object_guidance(item: dict[str, Any]) -> str:
    command = item.get("command")
    if command == "kol":
        return "\n".join(
            [
                "- [ ] 建立或更新 `03_Entities/` 中的 KOL 实体页。",
                "- [ ] 记录平台、账号、领域、可靠性观察。",
                "- [ ] 只有出现明确推荐时，才进入 KOL 推荐事件表。",
            ]
        )
    if command == "event":
        return "\n".join(
            [
                "- [ ] 核验六要素：KOL、平台、标的、方向、理由、原始链接。",
                "- [ ] 不满足六要素时，保持为候选或待补。",
                "- [ ] 满足后再写入 `04_Projects/KOL推荐事件表.md`。",
            ]
        )
    if command == "concept":
        return "\n".join(
            [
                "- [ ] 判断是否值得建立 `02_Concepts/` 概念页。",
                "- [ ] 写清定义、适用场景、误用风险、相关来源。",
                "- [ ] 概念不能单独作为买入依据。",
            ]
        )
    if command == "holding":
        return "\n".join(
            [
                "- [ ] 建立或更新持仓审计卡。",
                "- [ ] 补齐建仓时间、价格、买入理由、失效条件。",
                "- [ ] 下一次复盘时检查证据是否增强或削弱。",
            ]
        )
    return "- [ ] 判断是否需要提炼到 Concepts / Projects / Strategies\n- [ ] 如果涉及股票、KOL、公司或行业，补充实体链接"


def trim_block(text: str, limit: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n\n...[truncated]"


def slugify(value: str) -> str:
    text = value.strip()
    for char in INVALID_FILENAME_CHARS:
        text = text.replace(char, "-")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-. ")
    if not text:
        return "capture"
    return text[:80]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to allocate unique file path under {parent}")

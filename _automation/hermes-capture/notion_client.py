from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


class NotionError(RuntimeError):
    pass


def load_notion_key(env_files: list[str]) -> str:
    env_value = os.environ.get("NOTION_API_KEY")
    if env_value:
        return env_value.strip()

    for env_file in env_files:
        path = Path(env_file)
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "NOTION_API_KEY" and value.strip():
                return value.strip().strip('"').strip("'")

    raise NotionError("NOTION_API_KEY not found in environment or configured .env files")


def rich_text(value: str, limit: int = 2000) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": str(value)[:limit]}}]}


def title_text(value: str, limit: int = 2000) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": str(value)[:limit]}}]}


DEFAULT_FIELDS = {
    "title": "日期",
    "url": "URL",
    "content": "text",
    "author": "作者",
    "author_username": "作者用户名",
    "tags": "Tags",
    "info_type": "信息类别",
    "notion_date": "Notion日期",
    "status": "状态",
    "source_type": "来源类型",
    "project": "项目归属",
    "value_score": "价值评分",
    "obsidian_path": "Obsidian路径",
}


class NotionClient:
    def __init__(
        self,
        api_key: str,
        database_id: str,
        version: str = "2022-06-28",
        fields: dict[str, str] | None = None,
    ):
        self.api_key = api_key
        self.database_id = database_id
        self.version = version
        self.fields = {**DEFAULT_FIELDS, **(fields or {})}
        self._database: dict[str, Any] | None = None

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.notion.com/v1/{path.lstrip('/')}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Notion-Version", self.version)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise NotionError(f"Notion API {method} {path} failed [{exc.code}]: {body}") from exc
        except urllib.error.URLError as exc:
            raise NotionError(f"Notion API {method} {path} failed: {exc}") from exc

    def get_database(self) -> dict[str, Any]:
        if self._database is None:
            self._database = self.request("GET", f"databases/{self.database_id}")
        return self._database

    @property
    def properties(self) -> dict[str, Any]:
        return self.get_database().get("properties", {})

    def title_property_name(self) -> str:
        configured = self.fields["title"]
        if configured in self.properties and self.properties[configured].get("type") == "title":
            return configured
        for name, spec in self.properties.items():
            if spec.get("type") == "title":
                return name
        return configured

    def ensure_minimum_schema(self) -> list[str]:
        desired = {
            self.fields["status"]: {
                "select": {
                    "options": [
                        {"name": "Inbox", "color": "yellow"},
                        {"name": "已读", "color": "blue"},
                        {"name": "已提炼", "color": "green"},
                        {"name": "已迁移Obsidian", "color": "purple"},
                        {"name": "归档", "color": "gray"},
                        {"name": "丢弃", "color": "red"},
                    ]
                }
            },
            self.fields["source_type"]: {
                "select": {
                    "options": [
                        {"name": "X", "color": "blue"},
                        {"name": "小红书", "color": "red"},
                        {"name": "知乎", "color": "blue"},
                        {"name": "公众号", "color": "green"},
                        {"name": "抖音", "color": "orange"},
                        {"name": "B站", "color": "pink"},
                        {"name": "网页", "color": "gray"},
                        {"name": "PDF", "color": "brown"},
                        {"name": "视频", "color": "orange"},
                        {"name": "自己想法", "color": "purple"},
                    ]
                }
            },
            self.fields["project"]: {
                "select": {
                    "options": [
                        {"name": "知识管理", "color": "blue"},
                        {"name": "交易系统", "color": "green"},
                        {"name": "KOL指数", "color": "orange"},
                        {"name": "宝妈指数", "color": "pink"},
                        {"name": "AI工具", "color": "purple"},
                        {"name": "生活记录", "color": "yellow"},
                        {"name": "暂不处理", "color": "gray"},
                    ]
                }
            },
            self.fields["info_type"]: {
                "select": {
                    "options": [
                        {"name": "基础信息", "color": "gray"},
                        {"name": "知识型信息", "color": "blue"},
                        {"name": "日志", "color": "yellow"},
                    ]
                }
            },
            self.fields["value_score"]: {"number": {"format": "number"}},
            self.fields["obsidian_path"]: {"rich_text": {}},
        }

        missing = {name: spec for name, spec in desired.items() if name not in self.properties}
        changed = list(missing.keys())
        select_option_patches = self.missing_select_option_patches(desired)
        missing.update(select_option_patches)
        changed.extend(select_option_patches.keys())
        if not missing:
            return []

        self.request("PATCH", f"databases/{self.database_id}", {"properties": missing})
        self._database = None
        return changed

    def missing_select_option_patches(self, desired: dict[str, Any]) -> dict[str, Any]:
        patches: dict[str, Any] = {}
        for name, spec in desired.items():
            if name not in self.properties:
                continue
            if "select" not in spec or self.properties[name].get("type") != "select":
                continue
            current_options = self.properties[name].get("select", {}).get("options", [])
            current_names = {option.get("name") for option in current_options}
            wanted_options = spec.get("select", {}).get("options", [])
            additions = [option for option in wanted_options if option.get("name") not in current_names]
            if additions:
                patches[name] = {"select": {"options": current_options + additions}}
        return patches

    def find_by_url(self, url: str | None) -> dict[str, Any] | None:
        url_field = self.fields["url"]
        if not url or url_field not in self.properties:
            return None

        payload = {
            "filter": {"property": url_field, "url": {"equals": url}},
            "page_size": 1,
        }
        result = self.request("POST", f"databases/{self.database_id}/query", payload)
        rows = result.get("results", [])
        return rows[0] if rows else None

    def existing_obsidian_path(self, page: dict[str, Any]) -> str:
        prop = page.get("properties", {}).get(self.fields["obsidian_path"], {})
        values = prop.get("rich_text") or []
        return "".join(part.get("plain_text", "") for part in values).strip()

    def create_capture_page(self, item: dict[str, Any]) -> dict[str, Any]:
        props = self.capture_properties(item, include_url=True, include_date=True)

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": props,
            "children": self._page_blocks(item),
        }
        return self.request("POST", "pages", payload)

    def capture_properties(
        self,
        item: dict[str, Any],
        *,
        include_url: bool = False,
        include_date: bool = False,
    ) -> dict[str, Any]:
        title_name = self.title_property_name()
        props: dict[str, Any] = {
            title_name: title_text(item["title"]),
        }

        if include_url and item.get("url") and self.fields["url"] in self.properties:
            props[self.fields["url"]] = {"url": item["url"]}
        if include_date and self.fields["notion_date"] in self.properties:
            props[self.fields["notion_date"]] = {"date": {"start": datetime.now().strftime("%Y-%m-%d")}}
        if self.fields["content"] in self.properties:
            props[self.fields["content"]] = rich_text(item.get("content") or item.get("summary") or item.get("note") or "")
        if item.get("author") and self.fields["author"] in self.properties:
            props[self.fields["author"]] = rich_text(item["author"])
        if item.get("author_username") and self.fields["author_username"] in self.properties:
            props[self.fields["author_username"]] = rich_text(item["author_username"])
        if self.fields["status"] in self.properties:
            props[self.fields["status"]] = {"select": {"name": item.get("status", "Inbox")}}
        if self.fields["source_type"] in self.properties:
            props[self.fields["source_type"]] = {"select": {"name": item.get("source_type", "网页")}}
        if self.fields["project"] in self.properties:
            props[self.fields["project"]] = {"select": {"name": item.get("project", "知识管理")}}
        if self.fields["value_score"] in self.properties:
            props[self.fields["value_score"]] = {"number": int(item.get("value_score", 2))}
        if self.fields["tags"] in self.properties:
            tags = [item.get("source_type"), item.get("project"), item.get("object_hint")]
            props[self.fields["tags"]] = {"multi_select": [{"name": tag} for tag in tags if tag]}
        if self.fields["info_type"] in self.properties:
            props[self.fields["info_type"]] = {"select": {"name": item.get("info_type", "知识型信息")}}
        return props

    def update_capture_page(self, page_id: str, item: dict[str, Any]) -> None:
        self.request(
            "PATCH",
            f"pages/{page_id}",
            {"properties": self.capture_properties(item)},
        )

    def replace_page_blocks(self, page_id: str, item: dict[str, Any]) -> None:
        for block_id in self.list_child_block_ids(page_id):
            self.request("PATCH", f"blocks/{block_id}", {"archived": True})

        blocks = self._page_blocks(item)
        if not blocks:
            return
        self.request(
            "PATCH",
            f"blocks/{page_id}/children",
            {"children": blocks},
        )

    def list_child_block_ids(self, block_id: str) -> list[str]:
        ids: list[str] = []
        cursor = ""
        while True:
            query = "?page_size=100"
            if cursor:
                query += f"&start_cursor={cursor}"
            result = self.request("GET", f"blocks/{block_id}/children{query}")
            ids.extend(block["id"] for block in result.get("results", []) if block.get("id"))
            if not result.get("has_more"):
                return ids
            cursor = result.get("next_cursor") or ""

    def update_obsidian_path(self, page_id: str, obsidian_path: str) -> None:
        obsidian_path_field = self.fields["obsidian_path"]
        if obsidian_path_field not in self.properties:
            return
        self.request(
            "PATCH",
            f"pages/{page_id}",
            {"properties": {obsidian_path_field: rich_text(obsidian_path)}},
        )

    def _page_blocks(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        sections = [
            ("捕获摘要", item.get("summary", "")),
            ("补采任务", heavy_backend_block_text(item)),
            ("我的备注", item.get("note", "")),
            ("原始内容/快照", item.get("content", "")),
        ]
        for heading, text in sections:
            if not text:
                continue
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]},
                }
            )
            for chunk in split_text(text):
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                    }
                )
        return blocks[:90]


def split_text(text: str, max_len: int = 1800) -> list[str]:
    clean = (text or "").strip()
    if not clean:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in clean.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_len:
            if current:
                chunks.append(current)
            current = paragraph[:max_len]
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def heavy_backend_block_text(item: dict[str, Any]) -> str:
    if not item.get("heavy_backend_status"):
        return ""
    lines = [
        f"后端：{item.get('heavy_backend', 'MediaCrawler')}",
        f"状态：{item.get('heavy_backend_status')}",
    ]
    if item.get("heavy_backend_job_id"):
        lines.append(f"Job ID：{item['heavy_backend_job_id']}")
    if item.get("heavy_backend_queue_file"):
        lines.append(f"队列：{item['heavy_backend_queue_file']}")
    if item.get("heavy_backend_command"):
        lines.append(f"命令：{item['heavy_backend_command']}")
    if item.get("heavy_backend_error"):
        lines.append(f"错误：{item['heavy_backend_error']}")
    return "\n".join(lines)

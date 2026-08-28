# Codex 交接笔记

更新日期：2026-07-02

## 项目概述

`E:\aiworkspace\ai-hub` 是个人 AI 投研工作台的源码仓库。目标是把飞书捕获的信息，经过 Hermes 处理，保存到 Notion（原始资料库）和 Obsidian（知识库），最终支撑交易研究和另类数据指数（KOL 指数、宝妈指数等）。

## 本地关键路径

| 用途 | 路径 |
|---|---|
| 项目仓库 | `E:\aiworkspace\ai-hub` |
| Hermes 用户配置 | `C:\Users\YOUR_USER\.hermes` |
| Hermes 运行目录 | `C:\Users\YOUR_USER\AppData\Local\hermes` |
| Obsidian vault | `F:\research` |
| Notion 信息收集库 | `📘 信息收集` (database id: `REPLACE_WITH_NOTION_DATABASE_ID`, 真实 ID 通过环境变量 `NOTION_DATABASE_ID` 提供) |
| 外部第三方仓库 | `E:\aiworkspace\ai-hub\_external` |
| 运行时数据 | `E:\aiworkspace\ai-hub\_runtime` |

## 信息流主线（已打通，可用）

```
飞书 /clip URL
-> hermes-capture-commands plugin
-> capture_pipeline.py（识别平台、调用 reader、抓取正文）
-> Notion 信息收集库（原始资料 + 状态 + 项目归属 + 价值评分）
-> F:\research\00_Inbox（Obsidian Markdown）
-> 轻量 reader 失败 → 自动入队 MediaCrawler 补采队列
```

飞书指令：
- `/clip <url> [备注]` — 抓取内容，保存到 Notion + Obsidian
- `/idea <文字>` — 保存想法到 Notion + Obsidian
- `/readlater <url>` — 只保存 Notion，不写 Obsidian
- `/auto <分享文本>` — 有 URL 时按 `/clip`，无 URL 时按 `/log`
- `/log <文字>` 或 `/day <文字>` — 日常记录，只写 Notion
- `/kol <url> [备注]` — KOL 实体线索
- `/event <url> [备注]` — KOL 推荐事件候选，后续必须六要素审计
- `/concept <文字或url> [备注]` — 概念线索
- `/holding <文字>` — 真实持仓/建仓审计线索

## 核心脚本清单

| 脚本 | 用途 | 关键入口 |
|---|---|---|
| `capture_pipeline.py` | 主入口：解析消息、识别平台、调用 reader、写 Notion/Obsidian、失败自动入队 | `build_item()` → `read_source()` → `maybe_enqueue_mediacrawler()` |
| `notion_client.py` | Notion API：创建页面、按 URL 查重、更新属性、替换正文块 | `create_capture_page()` / `find_by_url()` / `replace_page_blocks()` |
| `obsidian_writer.py` | 写 Obsidian Markdown，frontmatter + 正文模板 | `write_note()` / `slugify()` |
| `reader_probe.py` | 轻量 reader 健康检查，不写 Notion/Obsidian | `--timeout 45 --json` |
| `backend_doctor.py` | 后端体检：CLI 工具、MCP、登录态、API key、本地仓库 | |
| `mediacrawler_runner.py` | MediaCrawler 命令生成器：dry-run / enqueue / execute | `build_job()` / `enqueue_job()` / `execute_job()` |
| `mediacrawler_queue.py` | 补采队列管理：list / run-next / run-id / mark | |
| `mediacrawler_importer.py` | 扫描 MediaCrawler 输出，导入 Obsidian + Notion | `scan` / `import --notion` |

测试文件：
- `tests/test_parse.py` — 消息解析和平台分类
- `tests/test_mediacrawler_runner.py` — 命令生成和队列入队
- `tests/test_mediacrawler_queue.py` — 队列管理
- `tests/test_mediacrawler_importer.py` — 导入器

## 平台抓取现状（最终结论）

### 已可用（/clip 实时链路可读正文）

| 平台 | Reader 方法 | 备注 |
|---|---|---|
| X/Twitter | `vxtwitter` → `fxtwitter` → `oEmbed` | 单条推文正文、作者、互动数据、引用推文均可读 |
| 公众号 | 微信移动端 UA + `#js_content` HTML 解析 | 标题、作者、正文 |
| 抖音 | `douyin-mcp-server` via `mcporter` | 元信息（标题、video_id、下载链接）可读，ASR 语音转文本缺 `DASHSCOPE_API_KEY` |
| B站 | 公开 API `x/web-interface/view` + 字幕接口 | 标题、简介、作者、播放/弹幕/点赞等元信息；字幕看视频是否提供 |
| 普通网页 | `r.jina.ai` | 通用网页正文抽取 |

### 不可用（需要登录态/外部凭证）

| 平台 | 当前症状 | 根因 | 修复方式 |
|---|---|---|---|
| 小红书 | `rednote` 未登录，`xhs` cookie guest/stale | 本机浏览器无小红书登录 cookie | 在 Chrome 登录 xiaohongshu.com → `xhs login --cookie-source chrome` |
| 知乎(轻量) | API 403 / Jina 安全验证页 | 知乎非登录态封挡 | 无法绕过，必须走重型后端 |
| 知乎(重型) | MediaCrawler CDP 连接成功，但 detail 仅支持 `/answer/`、`/p/`、`/zvideo/` 三种 URL | MediaCrawler parser 不识别 `/question/` 页 | 用具体回答/文章链接代替问题页 |

### 重型后端（MediaCrawler）状态

- 已拉取到 `_external/MediaCrawler`，依赖可用（`uv run main.py --help` 通过）
- XHS-Downloader 已拉取到 `_external/XHS-Downloader`
- `twitter-cli` 已装（未认证），`bilibili-cli` 已装（未登录）
- 补采队列：`_runtime/mediacrawler/queue.jsonl`（当前为空）
- 轻量 reader 失败时自动入队：`capture_pipeline.py` 第 851 行 `maybe_enqueue_mediacrawler()`
- `config.yaml` 控制：`enable_mediacrawler_queue: "true"`，触发平台 `小红书;知乎;抖音;B站`

Chrome CDP 启动方式（MediaCrawler 知乎已验证通过）：
```powershell
# 1. 关闭所有 Chrome 窗口
# 2. 用临时 profile + 调试端口启动
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\chrome-cdp-profile"
# 3. 在打开的 Chrome 里登录目标网站
# 4. 然后跑 MediaCrawler
```

补采命令：
```powershell
# 预览下一条 pending
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next

# 执行 + 导入 Obsidian
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute --import-results

# 执行 + 导入 Obsidian + Notion
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute --import-results --import-notion
```

## MediaCrawler 三层架构

```
mediacrawler_runner.py   ← 命令生成器（构造 uv run main.py --platform ...）
        ↓
mediacrawler_queue.py    ← 队列管理器（list / run-next / run-id / mark）
        ↓
mediacrawler_importer.py ← 结果导入器（扫描 JSONL → 合并正文+评论 → 写 Obsidian/Notion）
```

capture_pipeline.py 在轻量 reader 失败时自动调用 runner 的 `enqueue_job()`。

## 日常诊断命令

```powershell
# 后端工具和登录态体检
python E:\aiworkspace\ai-hub\_automation\hermes-capture\backend_doctor.py

# 轻量 reader 链接测试
python E:\aiworkspace\ai-hub\_automation\hermes-capture\reader_probe.py --timeout 45 --json

# 补采队列状态
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json list

# 本地测试（不写 Notion/Obsidian，无需外部依赖）
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_parse.py
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_mediacrawler_runner.py
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_mediacrawler_queue.py
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_mediacrawler_importer.py
```

## Hermes 配置要点

- Hermes 实际运行目录是 `C:\Users\YOUR_USER\AppData\Local\hermes`（不是 `.hermes`）
- Skill 放在 `skills\hermes-capture\SKILL.md`（两个目录都有副本，运行态读 `AppData\Local` 那份）
- 捕获主线是 plugin-first：`hermes-capture-commands` 直接注册 slash commands，旧 quick alias 不应再作为主路径
- Notion API key 从 `C:\Users\YOUR_USER\.hermes\.env` 读取，不要写入仓库
- Gateway 当前只开飞书，Telegram 已显式关闭
- Clash/Mihomo 代理：`127.0.0.1:7897`；ai-hub 仓库的 Git 已配本地代理

## 不要做的事

- 不要提交 `_external/`、`_runtime/`、密钥、token 到 Git
- 不要把 `F:\research` 整个 vault 提交
- 不要全量迁移 Notion 历史资料
- 不要在登录态补好之前用 `--execute` 跑 MediaCrawler（会弹浏览器但拿不到内容）
- `_external` 和 `_runtime` 已在 `.gitignore` 忽略

## GitHub

- 远端：`https://github.com/MenikeKassel/ai-hub`（私有）
- 本机 Git 代理：`http://127.0.0.1:7897`（Clash/Mihomo）
- 当前有未提交改动（capture_pipeline、notion_client、obsidian_writer 等的 reader 增强）

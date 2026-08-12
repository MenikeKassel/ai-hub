# System Map

## 模块组成

| 模块 | 路径 | 说明 |
|------|------|------|
| Hermes capture | `_automation/hermes-capture/` | 飞书/链接 → Notion 采集流水线；Obsidian 写入已停用（2026-08-06） |
| Codex delegate | `_automation/codex-delegate/` | Hermes → 本地 Codex CLI 执行桥（只读/写模式、沙箱与审批保持现状） |
| KOL 研究台 | `_automation/trading_research/` | 本地 A 股 KOL 采集、审核、收益审计（只读研究辅助，非交易系统） |
| Inventory | `_automation/inventory/` | Notion 导出清单与历史候选合并工具 |
| Skills | `_skills/` | Hermes/Codex 技能文档（canonical 副本） |
| Templates | `_templates/` | 配置与文档模板 |
| Scripts | `scripts/` | 安装与 doctor 脚本 |

## 本地 Windows 专属能力

以下功能依赖本机 Windows 环境，**不会**在公开 CI 中执行：

- KOL 采集（X/Zhihu 浏览器自动化、计划任务）
- 本地行情与收益回算（`_runtime/trading`、FreeStockDB junction）
- Hermes Gateway 插件/钩子安装
- Credential Manager（Windows keyring）凭据读写
- OCR 批处理（RapidOCR / Unlimited-OCR）

## 外部数据源 / 凭据依赖

- Notion API：需要 `NOTION_API_KEY`（环境变量）与数据库 ID（环境变量或配置文件）
- X (Twitter)：需要 `auth_token` + `ct0`（keyring，不入库不入仓）
- 行情数据：AKShare / BaoStock / FreeStockDB（运行时下载，不入仓）
- 板块 RPS：东方财富 / 同花顺公开接口

## 数据边界

- 仓库**不包含**运行数据：`_runtime/`、`_external/`、`_data/`、`secrets/` 均被 .gitignore 排除
- 不保存：Notion 导出、Obsidian vault、媒体文件、原始帖子快照、用户凭证、Cookie、Token
- 公开镜像由脱敏管道生成，`scripts/check_public_mirror.py` 在 CI 中强制检查

## 信息流（当前）

```text
飞书/链接 → Hermes capture → Notion 信息收集库
                              ↓
                    人工或半自动整理（项目/概念/策略）
```

KOL 研究台独立闭环：采集 → AI 审核 → 事件批准 → 收益审计，数据仅存本地。

## 统计口径（2026-08-06 起）

- 看多（long）事件进入 A 股收益、胜率和排名统计
- 看空（short）事件保留在审计记录中，不计入收益排名（A 股无法做空）
- 统计版本：`kol-performance-v3`

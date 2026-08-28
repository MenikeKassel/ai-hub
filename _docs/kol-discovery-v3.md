# 全平台 KOL 发现与收纳 v3

状态：已实现本地私有适配层，公共工作流由 `kol-audit-workbench` v0.2 提供。

## 边界

- 支持 X、知乎、小红书、抖音、Bilibili、微博、微信/RSS、雪球、淘股吧。
- 私有仓库只保存适配器、凭据读取和本地采集结果；公共仓库保存协议、状态机、API 与 UI。
- AI 评分固定使用 OpenCode Go `deepseek-v4-flash`，权重为专业度 35、历史记录 25、相关性 15、活跃度 15、身份质量 10。
- AI 评分不会创建 KOL、接受候选或生成收益事件。候选必须由人明确接受。
- 回填最多 100 条、最多 30 天。人工启用时间以前的内容只归档，不能进入收益审计。
- Hermes 只能调用固定的发现、查看、评分、接受、拒绝、重试、档案和回填动作，不能编辑 schema 或源码。

## 本地采集格式

每个平台使用两个 UTF-8 JSON 文件：

```text
_runtime/trading/kol-discovery/captures/<platform>/accounts.json
_runtime/trading/kol-discovery/captures/<platform>/content.json
```

`accounts.json`：

```json
{
  "accounts": [
    {
      "external_account_id": "uid-42",
      "handle": "value-research",
      "display_name": "价值研究所",
      "profile_url": "https://example.invalid/u/42",
      "bio": "A股基本面研究",
      "follower_count": 120000,
      "evidence": [
        {"evidence_type": "search_result", "url": "https://example.invalid/search", "excerpt": "公司研究"}
      ]
    }
  ]
}
```

`content.json`：

```json
{
  "items": [
    {
      "external_account_id": "uid-42",
      "external_item_id": "item-1001",
      "posted_at": "2026-08-09T10:00:00+08:00",
      "url": "https://example.invalid/item/1001",
      "text": "原始内容"
    }
  ]
}
```

跨平台账号主键是 `(platform, external_account_id)`，内容主键是
`platform:external_item_id`。采集器可以来自浏览器、合法 API、RSS 或手工导出，
但必须在写入上述文件前完成平台条款、权限与速率限制检查。

## 运行

```powershell
& .\scripts\start-kol-discovery.ps1 -NoBrowser
```

新版发现控制台默认仅监听 `127.0.0.1:8125`，旧版研究台继续使用
`127.0.0.1:8123`，两者不抢端口。新版复用现有 `_runtime/trading/kol/posts.db`、
媒体和事件数据；行情日线只读 `ASHARE_FOUNDATION_ROOT`（默认
`F:\ai-data\ashare`）当前发布版本，本地市场库仅保留证券跟踪状态。

Hermes 先执行 `start-discovery`，再使用 `platform-status`、
`discover-accounts`、`list-candidates`、`score-candidate`、
`reject-candidate`、`retry-candidate`、`kol-profile` 和
`fetch-kol`。这些动作固定连接 8125；旧版动作仍固定连接 8123。
候选人准入只能在本地 KOL 控制台由人工完成，Hermes 不暴露准入动作。

## 状态流

```text
new -> reviewing -> accepted
                 -> rejected -> new (retry)
                 -> duplicate
                 -> unavailable -> new (retry)
```

接受候选后才写入已验证 KOL 身份和 `enabled_at`。历史快照、候选证据、发现运行与游标均保留在 SQLite 中。

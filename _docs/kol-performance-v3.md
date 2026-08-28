# KOL 阶段性表现分析 v3

表现模块独立读取 `_runtime/trading/kol/events.csv` 与 `checkpoints.csv`，不修改基准价、每日收益或冻结检查点。结果快照写入同目录的 `performance.db`，按算法版本和输入哈希幂等保存。

> v2 → v3 变更（2026-08-06）：收益统计改为 **long-only**。看空事件保留在审计记录中，但不再进入收益、胜率、排名或任何批次样本。

## 口径

- 主分析只纳入 active/completed、已验证、事前、可执行的正式事件。
- **收益统计仅纳入看多（long）事件**；看空（short）事件被 `_outcomes()` 排除，不计入 `batch_count`、`samples`、超额收益、胜率、MAE/MFE 与排名。
- 看空事件仍在 `events.csv` 与 `daily_marks.csv` 中保留，供审计与事后复盘。
- 同一帖子中的多只股票先在 1W/1M/3M/6M 节点按等权平均，帖子批次只计一个样本。
- X 与知乎按稳定 `platform + kol_id` 分开；旧事件无法关联原帖时使用 `legacy:platform:name` 隔离身份。
- 近期 7/30/90 日按检查点 `trade_date` 过滤，避免未成熟推荐提前进入近期表现。
- 置信区间为确定性 Bootstrap 中位数区间，批次少于 5 个时不生成。

## 计数字段

指标输出统一提供以下字段（API 与前端共用同一口径）：

| 字段 | 含义 |
|------|------|
| `long_event_count` | 进入收益统计的看多事件数 |
| `short_event_count` | 被排除的看空事件数（审计保留） |
| `executable_long_event_count` | 可执行的看多事件数（扣除执行警告后） |
| `audit_event_count` | 审计保留的事件总数（long + short） |

`event_count` 字段已废弃，仅作兼容保留（等于 `audit_event_count`）；新代码与文档一律使用上述四个字段。

## 阶段

`样本中`、`观察中`、`初步排名`、`较可信`、`长期验证`分别对应 v3 计划中的批次和推荐日门槛。小样本仍显示收益、胜率、MAE、MFE，但没有正式名次。

## 命令

```powershell
python trading_cli.py kol-performance-doctor
python trading_cli.py kol-performance-refresh --as-of YYYY-MM-DD
python trading_cli.py kol-performance-backfill
python trading_cli.py kol-performance-report --weekly --notify
python trading_cli.py kol-performance-report --weekly --with-ai --notify
```

默认不调用 AI。`--with-ai` 只把聚合后的指标交给 OpenCode Go 中的 DeepSeek V4 Flash 生成解释；模型失败时保留确定性模板，不能改变指标、阶段或排名。

## 页面

本地工作台的 `#/performance` 是独立的 **KOL表现** 页面。支持平台、成熟节点和近期落地窗口筛选；原 `/api/kol-leaderboard` 只保留兼容结构，旧 `score` 永远返回空值。页面明确标注：看空事件不参与收益/排名，仅保留审计。

# KOL 阶段性表现分析 v2

表现模块独立读取 `_runtime/trading/kol/events.csv` 与 `checkpoints.csv`，不修改基准价、每日收益或冻结检查点。结果快照写入同目录的 `performance.db`，按算法版本和输入哈希幂等保存。

## 口径

- 主分析只纳入 active/completed、已验证、事前、可执行的正式事件。
- 同一帖子中的多只股票先在 1W/1M/3M/6M 节点按等权平均，帖子批次只计一个样本。
- X 与知乎按稳定 `platform + kol_id` 分开；旧事件无法关联原帖时使用 `legacy:platform:name` 隔离身份。
- 近期 7/30/90 日按检查点 `trade_date` 过滤，避免未成熟推荐提前进入近期表现。
- 置信区间为确定性 Bootstrap 中位数区间，批次少于 5 个时不生成。

## 阶段

`样本中`、`观察中`、`初步排名`、`较可信`、`长期验证`分别对应 v2 计划中的批次和推荐日门槛。小样本仍显示收益、胜率、MAE、MFE，但没有正式名次。

## 命令

```powershell
python trading_cli.py kol-performance-doctor
python trading_cli.py kol-performance-refresh --as-of YYYY-MM-DD
python trading_cli.py kol-performance-backfill
python trading_cli.py kol-performance-report --weekly --notify
python trading_cli.py kol-performance-report --weekly --with-ai --notify
```

默认不调用 AI。`--with-ai` 只把聚合后的指标交给 DeepSeek 生成解释；模型失败时保留确定性模板，不能改变指标、阶段或排名。

## 页面

本地工作台的 `#/performance` 是独立的 **KOL表现** 页面。支持平台、成熟节点和近期落地窗口筛选；原 `/api/kol-leaderboard` 只保留兼容结构，旧 `score` 永远返回空值。

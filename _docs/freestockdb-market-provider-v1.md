# FreeStockDB 行情适配器 v1

## 定位

FreeStockDB 在本系统中是可选的第三行情源和分钟线补充源，不是主数据源。

```text
BaoStock -> AKShare -> FreeStockDB
```

现有收益审计仍使用标准化市场仓库中的真实不复权日线。FreeStockDB 不会自动启动、同步或覆盖健康的标准日线。

## 配置

启动 FreeStockDB 本地服务后，默认读取：

```text
http://127.0.0.1:7899
```

也可以通过环境变量覆盖：

```powershell
$env:FREESTOCKDB_URL = "http://127.0.0.1:7899"
```

适配器只访问 loopback HTTP 接口，不保存 FreeStockDB 的凭据或数据集副本。

## 日线行为

- BaoStock 和 AKShare 都失败时，日线同步才尝试 FreeStockDB。
- 已存在健康 canonical 数据时，FreeStockDB 只保存原始快照和审计记录。
- `raw`、`qfq`、`hfq` 分开保存；复权因子缺失时明确失败，不把原始数据冒充复权数据。
- `market-audit --cross-check` 会将 FreeStockDB 与标准化日线比较，价格差异超过 0.5% 记录冲突。

## 分钟线行为

分钟线不进入 KOL 收益计算，也不改变推荐事件基准价。通过以下命令按需抓取并保存：

```powershell
python trading_cli.py market-freestockdb-doctor
python trading_cli.py market-minute-fetch --symbol 600900 --start 2026-07-01 --end 2026-07-03 --frequency 1m
python trading_cli.py market-minute-fetch --symbol 159139 --start 2026-07-01 --end 2026-07-03 --frequency 5m --adjustment qfq
```

标准化快照写入：

```text
_runtime/trading/market/raw/freestockdb/
_runtime/trading/market/warehouse/minute_<frequency>/
_runtime/trading/market/manifests/runs.jsonl
```

分钟线主要用于条件型推荐、盘中入场条件复核和后续技术研究。当前系统仍会把缺少分钟数据的事件标记为不可执行审计，而不是补造入场价格。

## 验收边界

首轮比较标的：`600900`、`603127`、`159139`、`000300`。需要检查日期覆盖、OHLC关系、成交量、复权因子和最近交易日是否一致。FreeStockDB 数据源的授权和再分发条件仍需使用者向数据提供方确认。

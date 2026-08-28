# FreeStockDB 更新超时修复 — Codex 交接单

日期：2026-07-31（周五）深夜
交接人：Hermes（会话 2026-07-31）
状态：待 Codex 执行

## 一、要改的代码（唯一任务）

**文件**：`E:\aiworkspace\ai-hub\scripts\freestockdb-update.ps1`
**位置**：第 51 行
**改动**：

```powershell
# 改前
$arguments = @($cli, "market-freestockdb-update", "--timeout", "900")
# 改后
$arguments = @($cli, "market-freestockdb-update", "--timeout", "3300")
```

**要求**：只改这一处，不动其他内容；改完 `git diff scripts/freestockdb-update.ps1` 确认只有这一行变化。

## 二、为什么改（背景）

1. Windows 计划任务 **FreeStockDB_Update_Daily**：工作日 18:20 触发，执行时间限制 1 小时，调用 `freestockdb-update.ps1`
2. 脚本调用 `trading_cli.py market-freestockdb-update --timeout 900`（900 秒 = 15 分钟）
3. 更新流程实际耗时远超 15 分钟：
   - `_stage_data()` 全量复制 11.85GB 数据到 staging（`shutil.copytree`，见 `_automation/trading_research/freestockdb_runtime.py` 第 789 行）
   - 在 staging 运行 `数据更新.exe` 全市场增量同步
   - SHA-256 校验 11.85GB 全部文件（`_verify_staged_data`）
   - swap（live→previous, staging→live）+ 重启服务 + smoke test
4. 结果：**7/28、7/29、7/30、7/31 连续 4 天超时失败**，数据停在 7/27
   - 证据：`_runtime/trading/market/logs/freestockdb-update.jsonl` 全部是 `timed out after 900.0 seconds`
   - `_runtime/trading/market/logs/freestockdb-update-task.log` 同
5. 3300 秒 = 55 分钟，在计划任务 1 小时限制内，留 5 分钟余量

## 三、2026-07-31 事故记录（重要背景，改代码时别碰数据）

- 今天 20:36 有人手动双击 `E:\aiworkspace\stockdb\数据更新.exe` 直接更新 live 数据
- **后果**：库从 11.85GB 缩水到 9.8GB；KOL 事件涉及的 142 只股票中 94 只数据全丢（含 601899、603650 等）
- **已恢复**：从 `E:\aiworkspace\stockdb\data.before-migration-20260728-105642`（7/28 迁移前备份，296 个 .ldb，9.6GB）恢复
- **恢复方式**：引擎已重启（stockdb.exe，端口 7899）；`D:\ai-data\free-stockdb\live` 已重建为 junction → `E:\aiworkspace\stockdb\data`
- **损坏副本保留**（勿删，确认无用后再清）：`D:\ai-data\free-stockdb\live.corrupt-20260731-2045`、`E:\aiworkspace\stockdb\data.corrupt-20260731-2045`
- **教训：永远不要手动直接跑 数据更新.exe 更新 live 数据，必须走 ai-hub 的 staging 流程**（`market-freestockdb-update`）
- 当前数据状态：恢复到 7/27 完整全市场库，**缺 7/28~7/31 四个交易日**，等 timeout 修复后跑一次正常更新即可补上

## 四、改完后的验证步骤（Codex 只改代码，验证由 Hermes/用户执行）

```powershell
# 1. 确认改动
git -C E:\aiworkspace\ai-hub diff scripts/freestockdb-update.ps1

# 2. 手动触发一次更新补数据（注意 PYTHONPATH 必须清空，否则加载错误的 numpy）
cd E:\aiworkspace\ai-hub
$env:PYTHONPATH = ""
_runtime\venv-trading\Scripts\python.exe _automation\trading_research\trading_cli.py market-freestockdb-update --timeout 3300

# 3. 验证数据最新日期（预期到 7/31）
# http://127.0.0.1:7899/?cmd=get&t=日k:600702:20260731
```

## 五、相关路径速查

| 用途 | 路径 |
|---|---|
| 更新脚本 | `E:\aiworkspace\ai-hub\scripts\freestockdb-update.ps1` |
| 定时任务 | `FreeStockDB_Update_Daily`（工作日 18:20） |
| 运行时管理 | `E:\aiworkspace\ai-hub\_automation\trading_research\freestockdb_runtime.py` |
| 更新日志 | `E:\aiworkspace\ai-hub\_runtime\trading\market\logs\freestockdb-update.jsonl` |
| 任务日志 | `E:\aiworkspace\ai-hub\_runtime\trading\market\logs\freestockdb-update-task.log` |
| 状态文件 | `E:\aiworkspace\ai-hub\_runtime\trading\market\freestockdb-state.json` |
| doctor | `PYTHONPATH="" _runtime\venv-trading\Scripts\python.exe _automation\trading_research\trading_cli.py market-freestockdb-doctor` |
| 数据源 | `http://a.123128.xyz`（sync_url.txt 配置，在线） |
| 引擎 | `E:\aiworkspace\stockdb\stockdb.exe`（端口 7899） |
| 数据根 | `E:\aiworkspace\stockdb\data`（= `D:\ai-data\free-stockdb\live` junction） |

## 六、可选后续优化（本次不做，记录备查）

- `_stage_data()` 的 `shutil.copytree` 复制 11.85GB 太慢，可改硬链接（RocksDB 的 .ldb 文件不可变，硬链接安全；仅 LOG/CURRENT/MANIFEST 需真复制）→ 更新可缩短到分钟级
- 需要时另开任务委托 Codex

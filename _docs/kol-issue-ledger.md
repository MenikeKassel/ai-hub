# KOL 研究台 · 运行问题台账（Issue Ledger）

- 维护：Hermes（桌面会话）· 建立 2026-09-13 · 最后更新 2026-09-13
- 对象：ai-hub `_automation/trading_research`（本地控制台 http://127.0.0.1:8123）
- 用途：把 KOL 研究台的**运行问题**集中管理 —— 未闭环（Open）→ 已修复（Resolved）→ 周期性风险（Watch），避免重复诊断、交接丢失。
- 用法约定：处理任何一项后更新本文件（状态 + 变更日志）；给 GPT / Codex 的问题单统一从 §1 取。
- 来源材料：`_reports/gpt-handoff-KOL-v4未提交变更与行情故障-20260912.md`；`_docs/operations-current.md`、`_docs/architecture-current.md`；会话 20260904_204057（晨报失败分析）、20260910_224503（行情崩溃 + AI 积压）、20260911_022008（Codex 修复摘要）、20260912_222519（v4 + 行情故障成文）。

---

## 0. 当前快照（2026-09-13 16:30 · 周日实测）

| 项 | 现状 |
|---|---|
| 控制台 8123 | ✅ running（health=true，Hermes gateway=running，operator=available） |
| 晨报 | 今日 **14:07 补跑完成**（delivery_status=late，属正常标记）；采集 status=partial（2 个 X 账号 TimeoutError） |
| AI 队列 | pending_ai=12 ｜ 新帖 40 ｜ 模型日预算按上海日历日计 |
| 行情（发布态） | ✅ as_of **2026-09-11**，published=904，raw/qfq=904/904 |
| 控制台行情视图 | latest/expected/published 均为 2026-09-11 ｜ lagging_symbol_count=0 ｜ market_status=current |
| FreeStockDB | state=healthy（repair_failures=0）；数据更新 9/11 18:01 完成（expected_trade_date=2026-09-10） |
| 今晚窗口 | KOL_Post_Fetch_Daily 19:00 ｜ Zhihu_Fetch_Evening 19:20（周日照常） |
| 收益 | ✅ 16:27 静默重算完成：events=1287，marks=42866，errors=0；周末新事件 KOL-1290/1291 正常等待 9/14 首个交易日 |
| 下一个行情窗口 | **9/14（周一）19:30** 自动任务；成功发布后立即触发收益，23:30 为收益兜底 |

> 备注：今日 13:05 一批启动/补跑把整套拉了起来（UI、FreeStockDB、Nitter、晨报管线补跑），13:14 另有一批补跑；其中若干返回码非 0，见 Watch-7。

---

## 1. 未闭环问题（Open）

当前无 Open 项。9/14 首次无人值守运行仍按 §3 Watch 观测。

### O-5 决策看板（GPT 评审 Q1–Q4）

| # | 问题 | 关联 | 状态 |
|---|---|---|---|
| Q1 | 9/11 行情恢复时机（立即重跑 vs 等周一） | O-1 | ✅ 已立即续跑并发布 |
| Q2 | 晚间连续失败的稳定性方案 | O-2 | ✅ 可续跑候选 + GIL 崩溃最多三次换进程续跑 |
| Q3 | 变更集拆提交方案 | O-3 | ✅ 已拆为研究台/行情、KOL Wiki、Notion 辅助、文档四个本地提交；未推送 |
| Q4 | 文档修正范围 | O-4 | ✅ 已按现行“收益自动、研究手动”修正 |

（评审报告已生成于 2026-09-12；Q1–Q4 已于 2026-09-13 全部闭环。）

---

## 2. 已修复 / 已缓解（2026-08-14 ～ 09-13 · 防重复诊断）

| # | 问题 | 修复 | 日期 / 证据 |
|---|---|---|---|
| R-1 | **FreeStockDB 数据冻结在 08-25**（更新链被自拒：识别误判 + 客户端过旧） | 升级 v0.3.5（msgpack API、双库原子交换、stop 协议）+ 服务识别修复 + 停机握手；验收：样本到 9/10、截面 7486/7563=98.98%、与 Baostock 收盘一致 | 9/10–9/11；`market/freestockdb-state.json`=healthy、`market/freestockdb-update-state.json` |
| R-2 | FreeStockDB `port_conflict` 误判（读不到高权限进程→判冲突） | "持久化 PID + 实际监听者 + 根目录"联合验证 | 9/10；Codex thread 01a08060，回归测试通过 |
| R-3 | 发布脚本失败现场被删（ps1 `finally` 无条件删 `.out/.err`；stderr 触发误中止） | 失败保留独立 stdout/stderr + 固定 UTF-8 容错解码 + traceback 进报告 | 9/13；提交 `c6cc2a6` |
| R-4 | Hermes ↔ 研究台连接误判（健康 API 正常却报端口冲突） | 以 API 判可用性 + 启动元数据验身份（`ownership_verified`）；重复 start 返回 `already_running` | 9/10；`hermes-kol-operator.ps1` + 10 项测试 |
| R-5 | `kol_operator market dry_run=true` 仍真实触发计划任务 | 改为只出预览，不再触发 | 9/10 |
| R-6 | 正式事件中误入 4 条候选 / 已排除记录 | 已清理（含备份） | 9/9；`_runtime/trading/kol/backups/purge-candidate-excluded-20260909-175633/` |
| R-7 | doctor 报 `returns: missing`（9/4） | 现行：`KOL_Return_Tracker_Daily` 在岗；行情成功后触发，23:30 兜底 | 9/4 发现 → 9/13 调度加固 |
| R-8 | AI 积压 58 条（9/10） | 有界积压清收：`kol-morning-run --skip-fetch --backlog-limit 150 --phase final` → 58→0；机制：计划任务编排器固定 `backlog_limit=0`，积压需走 CLI 清收 | 9/10；残余 16 条 9/01 残渣见 W-3 |
| R-9 | X 主账号被警告（8/14） | 采集不再用主账号；现架构 = 专用小号会话槽位 + fxtwitter 公开兜底 | 8/14 起 |
| R-10 | **9/11 行情缺口**（Baostock 在 675/914 时 GIL 致命崩溃） | 实测候选已有 675 只完整 raw/qfq；刷新候选内研究状态后续跑，最终发布 904/904，lagging=0；收益静默重算成功 | 9/13；`restore-reports/market-daily-publish-task.json`，回滚 `market-live-previous-20260913-161902` |
| R-11 | **发布阶段 Windows 拒绝目录切换** | 根因是 PowerShell 将仍打开的 stdout/stderr 放在待重命名的 `market/logs` 内；任务日志迁至同级 `market-task-logs`，实际原子发布通过 | 9/13；第一次续跑完整复现 WinError 5，迁移日志后同候选发布成功 |
| R-12 | 晚间行情与收益任务重叠、致命崩溃后只能人工重来 | 候选支持受控续跑；包装器识别 `Fatal Python error`/`PyEval_SaveThread` 并最多三次换新 Python 进程续跑；行情成功后触发收益，收益检测行情互斥锁，23:30 兜底 | 9/13；48 项定向回归通过 |
| R-13 | v4 文档仍写“收益/研究均关闭” | 修正为收益自动更新、研究分析手动 | 9/13；`_docs/kol-refactor-v4.md` |
| R-14 | **v4 生产代码未提交、无法按版本回滚** | 完整验证后拆分本地提交：研究台/行情 `c6cc2a6`、KOL Wiki `c7b23cc`、Notion 辅助 `2632067`、运行文档；未推送远端 | 9/13；后端 408 + 12 subtests、前端 14、构建、Wiki lint 全过 |

---

## 3. 周期性风险与观测项（Watch）

| # | 项 | 说明 / 关注点 |
|---|---|---|
| W-1 | **X 采集限流与缺口** | 常态：`gap_detected` 告警反复、部分账号 TimeoutError（9/13 有 2 个）、曾出现 `X reader rate limit` 暂停窗口。纪律：auth / 限流 / 供应商 / 本地预算四态区分，**不绕限流换源**；X 与知乎分母分开报。**红线：采集凭据不用主账号** |
| W-2 | 知乎通道 | 单批 CDP 预检 + 独立浏览器会话；`degraded` 属常态；缺口统计同上框架 |
| W-3 | LLM 依赖（OpenCode Go / opencode.ai） | 经代理时断时续 → 会拖挂晨报 / 分类（帖子采集不受影响）；2026-09-01 残渣 16 条为 provider 断连（不再自动重试） |
| W-4 | 晨报 "late" 补跑模式 | 错过 09:00 线后由补跑完成并标 late（正常标记，不影响数据）；关注频度 |
| W-5 | 周末 / 假期无人值守 | 行情候选现可在致命崩溃后自动续跑；仍需观察 9/14 首次无人值守运行及长假前状态 |
| W-6 | 行情健康语义 | 9/13 恢复后周末口径已为 `current`，`lagging_symbol_count=0`；继续观察节假日口径 |
| W-7 | 计划任务杂项返回码 | `KOL_FreeStockDB_Start` 已于 16:37 复跑为 result=0；Nitter 保持 shadow/unavailable，晨报与分类待 9/14 下一次计划运行复核，若复现升级为 Open |

---

## 4. 关键路径索引（证据锚点）

- 行情发布：`_runtime/trading/market/{manifests/published-symbols.json, recovery-mode.json, logs/market-daily-publish.log}`
- 运行报告：`_runtime/trading/restore-reports/`
- 候选目录：`_runtime/trading/market-live-candidate-*`（含冻结现场）
- 重构备份：`D:\aiworkspace\_kol-repair-backups\refactor-20260908-095119`
- 本期外部报告：`_reports/gpt-handoff-KOL-v4未提交变更与行情故障-20260912.md`

---

## 5. 变更日志（本台账）

- **2026-09-13** 建立：合并 9/04、9/10、9/11、9/12 四轮问题的结论；初版（Open 5 ｜ Resolved 9 ｜ Watch 7）。
- **2026-09-13 16:30**：关闭 O-1/O-2/O-4；修复可续跑发布、任务日志句柄占用和行情/收益重叠；发布 9/11 行情并完成收益重算。
- **2026-09-13 16:40**：关闭 O-3/Q3；完成四段本地版本冻结，完整回归通过，Open 清零。
- **2026-09-13 15:57** 用户将本台账移交 **Codex Desktop 线程**（`01a08060-5fb3-7f92-a624-755b4ba1b95f`）执行；Codex 开工计划：稳定性修复 → 9/11 行情恢复 + 收益重算 → 文档口径修正 → 计划任务异常核验 → 未提交变更边界审计。Hermes 跟踪中。

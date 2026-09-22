# KOL 研究台 · 运行问题台账（Issue Ledger）

- 维护：Hermes / Codex（桌面会话）· 建立 2026-09-13 · 最后更新 2026-09-22
- 对象：ai-hub `_automation/trading_research`（本地控制台 http://127.0.0.1:8123）
- 用途：把 KOL 研究台的**运行问题**集中管理 —— 未闭环（Open）→ 已修复（Resolved）→ 周期性风险（Watch），避免重复诊断、交接丢失。
- 用法约定：处理任何一项后更新本文件（状态 + 变更日志）；给 GPT / Codex 的问题单统一从 §1 取。
- 来源材料：`_reports/gpt-handoff-KOL-v4未提交变更与行情故障-20260912.md`；`_docs/operations-current.md`、`_docs/architecture-current.md`；会话 20260904_204057（晨报失败分析）、20260910_224503（行情崩溃 + AI 积压）、20260911_022008（Codex 修复摘要）、20260912_222519（v4 + 行情故障成文）。

---

## 0. 当前快照（2026-09-22 07:46 · 运行验收）

| 项 | 现状 |
|---|---|
| 控制台 8123 | ✅ `/api/ready`：ok=true，version=4.0.0 |
| Hermes 连接 | ✅ watchdog 隐藏启动；operator 以健康 API 与进程所有权判定实例 |
| 晨报 | ✅ Windows PowerShell 5.1 原始 UTF-8 重定向；`-Platform all -NoFetch -NoNotify` 实测 exit=0 |
| 行情（发布态） | ✅ as_of **2026-09-21**，published=969，raw/qfq=969/969 |
| 920 股票 | ✅ 920010/045/179/367/478/608/670/895 均发布到 2026-09-21；920010 raw/qfq 各 174 行 |
| 控制台行情视图 | market_status=current，969/969 |
| FreeStockDB | ✅ 07:39–07:46 更新成功；7899 可达；样本均到 9/21；截面 7506/7563=99.2463%，Baostock 对账差异 0 |
| 收益 | ✅ 23:30 重算：events=1411，marks=53655，errors=0；随后清理无基线历史残留 KOL-1298，正式事件现为 1410 |
| 研究分析 | 按运行策略保持手动更新 |
| 审核工作台 | ✅ 历史待办 114 篇 / 224 条草稿已清零；主界面只保留今日审核与下一晨报 |

> 备注：KOL-1298 没有 baseline、marks 或 checkpoints，长期停留在 `awaiting_market_data`。2026-09-22 已在完整备份后删除该正式事件及两条 pending context；源帖子和审批记录继续保留。

---

## 1. 未闭环问题（Open）

当前无阻断性 Open 项。行情、收益、FreeStockDB 和控制台均已完成运行验收；缓存显示与可选 shadow 服务留在 §3 观察。

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
| R-15 | **9/21 行情候选在末段失败，920 股票缺 raw/qfq** | BaoStock 登录失败改为进程级熔断；日历自动降级到腾讯；腾讯 qfq/成交量规范化；920 股票仅在历史 OHLC 一致时启用可审计 identity-qfq | 9/22；发布报告 ok/published=true，969/969，8 个 920 股票全部到 9/21 |
| R-16 | **PowerShell 吞掉 Python traceback / 晨报 JSON 乱码** | 行情和晨报包装器改用 `Start-Process` 原始 stdout/stderr 重定向；失败日志保留在原子发布目录外 | 9/22；晨报 exit=0；行情最终发布成功 |
| R-17 | **market doctor 有隐式写入** | 移除 doctor 中的 instrument seed，恢复只读诊断语义 | 9/22；市场回归测试 |
| R-18 | **Hermes watchdog 弹窗及工作区路径耦合** | 新增按脚本位置解析仓库根目录的 VBS 隐藏启动器；两个任务安装入口统一使用 `wscript.exe` | 9/22；计划任务实测 LastResult=0 |
| R-19 | **KOL 绩效页重复扫描全部事件** | 后端按 horizon 批量计算并复用上下文；前端仅在绩效页签打开时请求榜单 | 9/22；1411 事件计算约 1.54s，前端测试与构建通过 |
| R-20 | **FreeStockDB 本机更新链状态不清** | 保持 staging→验证→原子交换，不放宽 freshness；本机 v0.3.5 更新与 7899 服务完成实测 | 9/22；`freestockdb-update-state.json`=ok/updated，覆盖 99.2463% |
| R-21 | **历史事件 KOL-1298 长期无基线并反复等待行情** | 在完整备份后删除 1 条无 baseline/marks/checkpoints 的正式事件及 2 条 pending context；保留源帖子与审批审计；未发现候选/排除项混入或孤儿收益记录 | 9/22；`kol-doctor`、`kol-performance-doctor`、`kol-context-doctor` 均通过，正式事件 1410，KOL-1298 不再出现 |
| R-22 | **审核工作台历史待办长期堆积** | 备份后逐条核对 114 篇帖子；可验证草稿批准，其余按复盘、重复、非股票或证据不足拒绝；现有 active backlog 清零。UI/API/晨报/reprocess/repair 统一只允许今日与下一晨报窗口，历史仅保留审计读取 | 9/23；pending/ready/needs_attention 均为 0；架构与前后端回归通过 |
| R-23 | **新增 KOL 的旧帖仍等待 AI/草稿生成** | ids 121–150 的 76 条 failed/not_requested 均不在两个活跃窗口，标记为 `archive-only-v1 / historical_archive_only`，不再生成历史待办；另由 Luna 逐篇核对并关闭今日 7 条 provider 失败（均为复盘、方法论或非明确个股推荐） | 9/23；76/76 归档；9/23 与 9/24 pending/failed 均为 0 |
| R-24 | **X 采集被 "no verified X session" 整体阻断**（09-21/22 限流后三槽位全部 `cooldown`+`enabled=0` 未复位；且验证子进程未传 `TWITTER_PROXY` → twitter-cli 直连 x.com 超时，槽位无法经 UI/API 恢复） | `sessions.py` 验证流程注入代理解析链（KOL_X_PROXY→TWITTER_PROXY→默认 127.0.0.1:7897）；带代理手动验证复位三槽位后 07:27 续跑 **46/83** 号成功并触发真实 X 限流（系统按设计冷却+全局暂停至 09:33，不绕限流）；09:33 单次自动续跑剩余 36 号 + 定向批 11 个新号；控制台已重启加载修复 | 9/23；`f11e40b`（含回归测试）、`x_session_slots`=ready×2/cooldown×1、`kol-post-fetch` 日志、`x-resume-20260923.log` |

---

## 3. 周期性风险与观测项（Watch）

| # | 项 | 说明 / 关注点 |
|---|---|---|
| W-1 | **X 采集限流与缺口** | 常态：`gap_detected` 告警反复、部分账号 TimeoutError（9/13 有 2 个）、曾出现 `X reader rate limit` 暂停窗口。纪律：auth / 限流 / 供应商 / 本地预算四态区分，**不绕限流换源**；X 与知乎分母分开报。**红线：采集凭据不用主账号** |
| W-2 | 知乎通道 | 单批 CDP 预检 + 独立浏览器会话；`degraded` 属常态；缺口统计同上框架 |
| W-3 | LLM 依赖（OpenCode Go / opencode.ai） | 9/23 完整晨报记录 HTTP 402，但当日 7 条失败已由 Luna 读原文后逐项关闭，当前审核队列 failed=0。后续 provider 可用性失败保留审计并等待服务恢复；历史帖不会重新进入待办 |
| W-4 | 晨报 "late" 补跑模式 | 错过 09:00 线后由补跑完成并标 late（正常标记，不影响数据）；关注频度 |
| W-5 | 周末 / 假期无人值守 | 候选可续跑且日历有腾讯与持久化审计回退；继续观察长假口径与供应商可用性 |
| W-6 | FreeStockDB 健康缓存 | 直接 doctor 已 exit=0/data_fresh=true，但 API 健康缓存暂显示 `checking`；计划任务元数据仍保留上次失败码 1，等待下一次调度刷新 |
| W-7 | 可选 Nitter shadow | Docker Desktop 被本机不可访问的 `dockerInference` AF_UNIX reparse point 阻断；主研究台不依赖该 shadow 服务 |
| W-8 | AKShare 代理 | 仍有代理错误；当前 BaoStock 主链路和 Tencent/FreeStockDB 降级链已通过，不阻断发布 |
| W-9 | 新增 X KOL 首次抓取 | ids 121–150 中 19 个成功；9 个尚未抓取、2 个 NotFound。一次定向抓取被 `no verified X session is currently available` 阻断，队列 attempts=0；启用经验证会话后再续跑，不循环重试 |

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
- **2026-09-22 07:46**：完成残留问题收口。发布 2026-09-21 行情 969/969，补齐 8 个 920 股票；收益重算 1411 事件且 0 错误；FreeStockDB v0.3.5 本机更新成功并通过覆盖率与 Baostock 对账。Hermes、晨报、只读 doctor、绩效页性能和失败取证同步加固。
- **2026-09-22 23:40**：备份后清理唯一异常历史正式事件 KOL-1298，并移除其 2 条 pending context。事件总数 1411→1410；marks/checkpoints 无孤儿，无需删除；源帖子和审批记录保留。
- **2026-09-23 02:07**：历史审核待办清零并退役 backlog 写入路径；工作台只保留今日审核/下一晨报。新增 KOL 的 76 条窗口外未完成帖子转为可审计历史归档；定向抓取受无已验证 X 会话阻断，仅保留一次未执行队列。
- **2026-09-23 02:29**：Luna 逐篇核对今日 7 条因 HTTP 402 失败的帖子，均为复盘、方法论、板块观点或重复内容，不构成可核验的当前个股推荐；全部保留原文审计并关闭，今日/下一晨报 pending=0、failed=0。
- **2026-09-23 07:55**：修复 X 会话验证缺代理（`f11e40b`，控制台已重启加载）；三槽位复位 ready 后恢复 X 采集，46/83 号成功后触发真实限流保护（冷却+全局暂停至 09:33），09:33 自动单次续跑剩余 36 号与定向批 11 号。OpenCode Go 新 API 已生效（分类通道恢复）。

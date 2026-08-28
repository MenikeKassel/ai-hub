# Handoff: ai-hub KOL 工作台升级（交给 Codex）

> 交接时间：2026-08-07 ｜ 交接人：Hermes ｜ 接收方：Codex
> 目标：在**不破坏现有 368 测试全绿**的前提下，继续升级 `_automation/trading_research`（KOL 研究台）。

---

## 一、项目背景

- 仓库：`E:\aiworkspace\ai-hub`（本地唯一真相源，分支 `main`，113 commits）
- 子系统：`_automation/trading_research` = KOL 审计工作台（FastAPI + SQLite/DuckDB + Parquet + React 前端）
- 本质：**A股新手防亏与 KOL 研究审计系统**，非自动交易/荐股系统
- 公开镜像：30 分钟管道脱敏后 PR 到 `MenikeKassel/ai-hub`（本地提交会自动同步，**无需手动推送**）
- KOL 研究台 UI：`http://127.0.0.1:8123`（uvicorn 常驻）

## 二、已完成（2026-08-06 ~ 08-07，勿重复做）

### 1. 多方法研究（event_research 五透镜）修复（提交 f199952/f098de7/614c4ee/fd0696d/df234d3）
- `event_research.py`：limit_streak 忽略 `pct_change` 的 inf/NaN（前收盘=0 时）
- `event_research_service.py`：截面日期缓存加 **60s TTL**（外部定时任务写入新 snapshot 后可见）
- `market_indicators.py`：13 个指标列计算后统一 inf→NaN 清洗
- `market_cross_section.py`：is_st 数值型 1.0 正确识别（原 str(1.0) 误判 False）；pct_change 除零清洗
- `event_dossier.py`：change_pct/amplitude_pct/volume_ratio_5 inf 清洗
- `event_context.py`：**零收盘除零产生 inf 污染 JSON（Infinity 非法 JSON）**——return_20d/distance_60d_high 输出 None + np.errstate
- `interpret_pending`：max_items=0 语义 = 处理 0 个（原为 1 个）

### 2. KOL 工作台核心修复（3cb87e1/eba1e55）
- `kol_posts.py` RuleClassifier：**中性词误判**——"风险"移出 SHORT_WORDS、"关注"移出 LONG_WORDS；方向判定改为"严格多数 + 平局为空"
- `kol_tracker.py` `_format_number`：inf/-inf 输出空字符串（原写字面量 `"inf"` 污染 CSV）

### 3. 榜单数据质量（6de7c1b）
- `kol_leaderboard.py`：`_number` 空/非数值/inf → None（原 0.0 造成**假 win_rate/样本虚增**）；`_metrics` 过滤无效值、全无效→samples=0；`_score` None 防护

### 4. 板块主线（board_mainline）彻底删除（52d4c69/026d00d/c382ca5/7dfaaf3）
- 删除：`board_mainline.py`(1393行)、`tests/test_board_mainline.py`、UI 页面 BoardMainline.tsx + BoardRpsChart + BoardRankTrendChart、kol_api 6 个 `/api/board-mainline/*` 路由、trading_cli 5 个 `market-board-*` 命令、`scripts/board-mainline-sync.ps1`、`_docs/board-mainline-rps-v1.md`、计划任务 Daily/Weekly
- 清理：install-research-data-tasks.ps1 任务注册、hermes-kol-operator.ps1 board 命令、hermes_operator_plugin.py ACTIONS、publish-public-system.ps1 safeDocs、README
- **保留**：market_data.py 的 board 表结构（board_catalog/board_daily/board_rps/board_memberships）——`event_research_service._board_context` 仍读取（leader lens 用），空表时优雅降级 warning

### 5. 工程基建（更早，已合并）
- `market_data.py` `write_daily`：空 frame 静默返回 []、缺 trade_date 列清晰 ValueError（30c0cde/fbed779）
- `test_hermes_operator_plugin.py`：补 sys.path 注入（此前从 tests/ discover 会漏跑其 12 个测试）（7dfaaf3）
- 公开 CI：`.github/workflows/public-ci.yml`（4 jobs：python/frontend/security/mirror）
- `scripts/check_public_mirror.py`：镜像卫生检查（git ls-files 只扫跟踪文件；.md 文档本地路径豁免——发布时统一替换；YOUR_USER 占位豁免）
- 路径统一：AI_HUB_HOME 环境变量优先（codex-delegate/install 脚本/config.example.yaml）
- 私有标识清理：Notion UUID、menike 真实用户名已全部从运行代码移除（环境变量/示例占位）
- 依赖锁定：`requirements.lock`（10 包精确版本，CI 用它安装）
- long/short 口径：收益只算 long，四计数字段 long/short/executable_long/audit 全链路输出

## 三、当前状态

- **测试：368 全绿**（`cd _automation/trading_research/tests && PYTHONPATH= 用 venv-trading python 跑 discover`）
- 前端：14 测试通过、tsc 无错误、build 成功
- 工作树：干净（仅 2 个未跟踪用户计划文档 `_docs/lianban-local-plan-v1.0.md`、`_docs/tomorrow-leads-plan-v0.1.md`，**勿提交/勿动**）
- 服务运行中：uvicorn `kol_api:app` @ 127.0.0.1:8123

## 四、升级候选方向（按优先级）

### P0 - 数据质量（最易出真 bug，延续既有模式）
1. **全仓除零/inf 审计**：搜索 ` / `、`/=`、`pct_change`、`/ len(` 等模式，凡分母可为 0/NaN 处统一 `_finite` 风格清洗（已有 12 个修复是模板）。重点：`kol_intraday.py`（`prices / first_price`）、`board_mainline` 已删但 `market_cross_section` 的 `close/preclose`、`review_agent` 的 `agreement` 类比率
2. **CSV/JSON 写入前有限性检查**：`_format_number` 模式已修复 kol_tracker，检查 `public_dataset.py`、`kol_performance.py` 的序列化路径
3. **datetime 解析健壮性**：`fromisoformat` 对 `Z`/无时区/`+08:00` 三种格式的容错（`_utc_and_local` 已修，查 `morning_orchestrator`、`pipeline_jobs`）

### P1 - 并发与缓存
4. `event_research_service` 的 `_daily_cache`/`_cross_section_cache` 字典：无锁，多线程 API 下可能竞态（FastAPI 默认线程池）——考虑 per-key 锁或 ThreadLocal
5. `FileLock` 超时值审计：`timeout=120` 的 pipeline-refresh 锁 vs 子任务 3×重试，确认无死锁窗口

### P2 - 功能升级
6. **多方法研究增强**：`_trend_structure` 单调趋势无 pivot 时返回 undetermined（设计限制）——可加 MA 斜率 fallback 但仍保持"不确定"语义
7. `validate_model_payload` 严格字段集（`set(value) != required`）：AI 输出多余字段整包拒绝——可考虑"记录警告+保留核心字段"的降级
8. 前端：ReviewQueue/EventManagement 大组件性能（虚拟列表），`board` 删除后 e2e console.spec 已同步（8→7 导航）

### P3 - 文档/发布
9. `_docs/system-map.md` 已重写；确认 `README.md` 与模块变更同步
10. `check_public_mirror.py` 在 CI 中跑通（本地 OK，CI 未实测）

## 五、环境与命令（必须遵守）

```bash
# 测试（系统 python 无 pandas/fastapi，必须用 venv）
cd E:/aiworkspace/ai-hub/_automation/trading_research/tests
export PYTHONPATH=
E:/aiworkspace/ai-hub/_runtime/venv-trading/Scripts/python.exe -m unittest discover -s . -p "test_*.py"
# 全量约 2-3 分钟

# 前端
cd E:/aiworkspace/ai-hub/_automation/trading_research/ui
npx tsc --noEmit && npm test && npm run build

# 服务启动（当前已在跑，重启需先 kill 8123）
cd E:/aiworkspace/ai-hub/_automation/trading_research
PYTHONPATH= E:/aiworkspace/ai-hub/_runtime/venv-trading/Scripts/python.exe -m uvicorn kol_api:app --host 127.0.0.1 --port 8123

# 健康检查
curl -s http://127.0.0.1:8123/api/system/health
```

## 六、硬性边界（不可违反）

1. **不改运行数据**：`_runtime/trading/` 下的 performance.db、events.csv、daily_marks.csv、checkpoints.csv、market/ 仓库数据
2. **不提交**：secrets、Notion 导出、`_runtime/`、`_external/`、Obsidian vault、2 个未跟踪计划文档
3. **不恢复**：second-brain、board_mainline、`/wiki` `/kb-*` 命令、旧守卫脚本
4. **核心/口径改动 Hermes 直写**；独立模块可用子代理（每步 git 提交，独立分支，验收后合并）
5. 看空不进收益（long-only），看空保留审计——**不要改回**
6. 每个修复：先写失败测试 → 修复 → 全量回归 → 独立提交（中文提交信息）
7. 除零/inf 修复必须带 `math.isfinite`/`np.isfinite` 检查 + 回归测试
8. **不要**大规模拆分 kol_api.py / capture_pipeline.py / kol_tracker.py

## 七、关键文件地图

| 模块 | 路径 | 说明 |
|------|------|------|
| 事件研究核心 | `event_research.py` (819行) | 五透镜分析引擎 |
| 事件研究服务 | `event_research_service.py` (894行) | build/缓存/interpret 编排 |
| AI 验证器 | `event_research_ai.py` (475行) | 正则防未来泄露/指令/代理过度声称 |
| 帖子采集/分类 | `kol_posts.py` (5182行) | 最大模块，X/Zhihu provider + RuleClassifier |
| 事件跟踪 | `kol_tracker.py` (1494行) | events.csv/收益计算 |
| 市场数据 | `market_data.py` (2633行) | Parquet 仓库 + DuckDB 元数据 |
| 指标计算 | `market_indicators.py` (97行) | RSI/ATR/MACD（inf 已清洗） |
| 截面龙头 | `market_cross_section.py` (414行) | leader lens |
| 榜单 | `kol_leaderboard.py` (183行) | long-only 四计数 |
| 表现统计 | `kol_performance.py` (954行) | v3 long-only |
| API | `kol_api.py` (2458行) | FastAPI 全部路由 |
| CLI | `trading_cli.py` (3930行) | 全部命令 |
| 前端 | `ui/src/` | React + TS（ReviewQueue/EventManagement/KolPerformance/Kols/System/StockLeads） |
| 测试 | `tests/` (35 文件) | unittest 风格 |

## 八、suggested skills

- `kol-research-operator`（KOL 工作台运维：启动/doctor/采集/审核）
- `a-share-selection-lab-ops` / `ashare-lab-walk-forward`（相关 A 股实验，如需对照）
- `a-share-research-framework`（A股研究框架，若升级涉及选股逻辑）
- `systematic-debugging`（4 阶段根因调试）
- `test-driven-development`（先失败测试后修复，本项目强制）
- `git-repo-archive`（如涉及归档，勿用）
- 模型策略：规划用 KIMI3/GLM-5.2、执行用 deepseek-v4-flash（OpenCode Go 订阅）；复杂任务可 `claude -p`（走 opencode.ai zen/go）

## 九、验收标准

1. 全量 368+ 测试通过（新增测试只增不减）
2. 前端 tsc + npm test + build 通过
3. `scripts/check_public_mirror.py` 输出 OK
4. 每个修复独立提交，中文信息，含回归测试
5. 不触碰边界列表中的任何数据/文件

---

*交接完成。祝升级顺利。*

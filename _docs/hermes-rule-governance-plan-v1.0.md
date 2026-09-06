# Hermes 规则治理审阅：实测核验与处置方案

- 文档版本：v1.0（2026-09-06 初始）
- 性质：对第三方静态审阅（15 条发现）的逐条实测核验 + 首批处置批次方案
- 状态：**讨论期，未执行任何文件修改**。等用户明示「开始」后按 Phase 顺序实施。

## 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-06 | 初始：15 条审阅逐条核验（✅/⚠️/❌）+ 修正 + 处置批次 |

## 一、审阅来源与核验方法

- 审阅方：第三方静态审阅（原文见会话记录，2026-09-06 转达），声称筛查了安装目录 201 个非归档 Skill、精读 Hermes 源码 AGENTS.md / SOUL.md / Codex 插件规则。
- 核验方法：对每条引用的 **文件:行号** 逐一读取原文比对；对存在性声明（AGENTS.md、模板、插件缓存）用 `ls`/`find` 实测；对数量声明（201 个 Skill）重新统计。
- 核验者必守：本地 git 为唯一真相源；讨论期只读探查，不写任何文件（本文档除外）。

## 二、逐条核验结论

图例：✅ 确认成立　⚠️ 部分成立/需修正　❌ 不成立

| # | 发现 | 结论 | 关键证据（实测原文） |
|---|---|---|---|
| 1 | 安装版与规范源漂移（路径 + KOL 行情同步 + capture） | ✅（且比审阅更严重） | 安装版 delegate-to-codex L40-41 默认工作区 `E:\aiworkspace\ai-hub`；源 `_skills` L12 为 `D:\aiworkspace\ai-hub`。E: 盘实测不存在。KOL 安装版 L59 列 `market`: synchronize market data（L60-65 另有 30 分钟 data-refresh）；源 L50-52 明确「Routine operator actions must not enable market synchronization」。安装版 capture 89 行（E: 路径 + /wiki + F:\research）；源 28 行（$env:AI_HUB_HOME 可移植路径）。`_templates\hermes\SOUL.codex-routing.md` 缺失（整目录不存在，git 未跟踪）；install-hermes-codex-delegate.ps1 L28 预检会 throw |
| 2 | 规划流程「已要求执行却仍要再问」闭环 | ✅ | plan-first-workflow L15「Any idea → plan first → then execute」、L48 调用 plan skill；plan skill L20「planning only」、L318 固定「Shall I proceed?」 |
| 3 | 批准放在整个委派之前，准备结果做不出来 | ✅ | SOUL.md L24-26 委派前须获批（含 login/credential）；安装版 delegate-to-codex L29-31 同义（含 use credentials）。确认「先准备后获批」的调整方向属于**调整批准时点，不自动授权最终操作** |
| 4 | 执行路由互相打架 | ✅ | delegate-to-codex L35-36「只能用 codex_delegate，禁用 terminal/execute_code/delegate_task」；独立 codex skill L16「via the Hermes terminal」；plan-first-workflow L56-57 指定 delegate_task/execute_code。L12 允许用户同意后回退 claude-code；L62「Codex 不可用或额度不足时不得回退」——确实并存。health-report-ingestion L9「本项目 Hermes 直接实施，不用 Codex」为真实项目例外 |
| 5 | KOL「停止操作」vs「结束任务」未分清 | ✅ | KOL 安装版 L41-42「report the structured error and stop」与 L50-51「use delegate-to-codex for maintenance」并存；L31「triggered」定义正确但无后续跟踪责任 |
| 6 | 进度询问/外部内容被误当授权 | ✅ | health L34「好了吗」= 授权批量补录（L18 却要求只有人工确认的行才写 Notion）；multi-agent-plan-review L74「任务书 = 执行授权」；plan-consultation-loop L12 正确强调「转达 ≠ 授权执行」但把「定稿」列为执行信号（multi-agent-plan-review L25 同） |
| 7 | 知识库字段被同时要求填写、禁止填写 | ✅ | notion-workspace-search L54「必须填 保存原因，缺它不建页」；karpathy-wiki-method L136「禁写 保存原因/处理状态/确认归宿（用户明确命令时代执行）」、L103「AI 永不置 consumed」、L189「consumed = 用户授权后」并存。例外已有，但措辞冲突 |
| 8 | 研究轮次「汇报」与「等待批准」混在一起 | ✅ | agent-based-market-research L85「停下汇报，等用户拍板下一轮机制」；abm-financial-markets L117「等拍板再继续」+ L131「用户拍板后可在授权链内连续多轮」并存 |
| 9 | Codex Notion skill 固定采访 + 连接恢复后再结束 | ✅ | notion-knowledge-capture L35「Ask purpose, audience, freshness…」、L32「finish your answer and tell the user to retry」；meeting-intelligence L33 同。审阅引用的路径/版本与实装一致 |
| 10 | Notion「规格到实现」只建任务卡 | ✅ | 描述 L3 声明用于「implementing PRDs/feature specs」，流程终点 L55-58 是「Track progress」，无实现交接或完成判据 |
| 11 | UI 规则需区分有意再批与意外逐点击 | ✅ | confirmations.md L34「Always Confirm at Action-Time (Even If Pre-Approved)」、L68「initial prompt」预授权（把后续消息中的授权排除在外——确认）；Hermes computer-use L260-262「anything the user didn't explicitly ask for」。注意：审阅引用版本号 26.901.41600，实装为 26.901.51231，内容一致 |
| 12 | grilling 面试触发范围过宽 | ✅ | grilling L3/L8 触发词含「帮我挑毛病/这方案有什么问题」，L15「一次只问一个问题」 |
| 13 | 强制先用某工具，无不可用出口 | ✅ | gift/AGENTS.md L4「ALWAYS use the code-review-graph MCP tools BEFORE Grep/Glob/Read」且实装 MCP 清单无此工具；openai-docs SKILL.md L12 官方文档优先于检查本地文件 |
| 14 | 「停止盲目重试」写成停止一切调查 | ✅ | systematic-debugging L295-301 Rule of Three「STOP…DON'T attempt Fix #4 without architectural discussion」；github-pr-workflow L275「up to 3 attempts, then ask the user」 |
| 15 | 审批拦截后建议互相矛盾 | ✅ | windows-msys-shell L81「拆成单文件通常不触发…这是缩小操作范围,不是绕过 consent」；L100「被拦后不要重试、不要换写法绕」；computer-use L287「Break the command up or reconsider」——三者确实互相矛盾 |
| — | 不存在 AGENTS.md 的声明 | ✅ | `C:\Users\12973\.codex\AGENTS.md`、`D:\aiworkspace\AGENTS.md` 均实测不存在；Hermes 个人规则在 SOUL.md |
| — | 201 个非归档 Skill | ✅ | `find … -name SKILL.md -not -path "*/.archive/*"` = 201（.archive 内另 12 个） |
| — | hermes-agent 源码 AGENTS.md「核实是否有意设计」 | ✅ | 位于「Before you call it a bug — verify the premise」节，属工程评审规则，非「直接做」 |

## 三、对审阅的修正与增补（本核验独有发现）

1. **同类漂移不止审阅列的三处**（均属第 1 项主题）：
   - health-report-ingestion L9 项目路径 `E:\aiworkspace\family-health`（实测在 D:，E: 已消失）；
   - multi-agent-plan-review L22 方案文档目录 `E:\aiworkspace\ai-hub\_docs`（实测在 D:）；
   - 安装版 hermes-capture L38 `F:\research` 实测不存在；L46 `/wiki` 指向已终止的第二大脑（2026-08-06 终止）与 `E:\aiworkspace\obsidian-vaults\research-os`。
2. **部署机制存在，盲目重跑会丢护栏**：三个 install 脚本（install-hermes-codex-delegate / install-hermes-capture / install-hermes-kol-research）均为 `_skills\<名>\SKILL.md` → `%LOCALAPPDATA%\hermes\skills\<名>` 强制覆盖。若直接重跑：
   - codex-delegate 脚本**必然在预检失败**（L28 检查缺失的 SOUL 模板）；
   - 即使补好模板，重跑会把 64 行安装版替换成 22 行源版，**丢失安装版的更强护栏**：L49「不得自行完成」、L61「不得自行检查/修改源码」、L62「不可用/额度不足不得回退」、L60 交易禁令。源版仅有 L22「失败报错不编造结果」。→ 必须先并入源文件，再部署。
3. **KOL 行情禁令的方向确认**：git log 显示 `_skills` 最后改动为 841aed5（2026-08-28），源版禁令（2026-08-25 截止）是**新规则**；安装版列市场同步是**旧规则**。按「本地 git 唯一真相源」约定，唯一合法同步方向是 源→安装，即**收紧**。审阅的「重新开放属于扩权」警告对应禁止反向拷贝（安装→源）与禁止以安装版为准。
4. **中央路由的执行细节**：plan skill L320-324 还指定执行走 `subagent-driven-development` + `delegate_task`，与 SOUL.md 路由冲突再深一层；统一时需连 plan skill 一并处理（或按审阅建议「执行方法服从中央路由」，通用 Skill 不再定义第二套入口）。
5. **第 6 项的「定稿」口径有双向用法**：plan-consultation-loop L12 与 multi-agent-plan-review L25 都把「定稿」与「开始/实施」并列当执行信号；但用户 profile 惯例是「须明说开始才执行」。审阅建议（定稿=方案完善授权，实施需上下文明示）与用户惯例一致，作为**收紧**采纳，需用户拍板确认口径。

## 四、处置分类（× = 需单独审阅的扩权项）

| 类别 | 条目 |
|---|---|
| 收紧（安全方向） | 2 规划闭环；3 批准时点后移；5 KOL stop 语义；6 「好了吗」收紧；7 例外措辞写清；12 grilling 触发条件；15 拆命令绕过条款统一 |
| 中性（工程一致性） | 1 漂移修复（源→安装部署 + 版本标识 + 一致性校验）；4 中央路由；8 轮次措辞；13 工具不可用出口；14 三次后证据整理 |
| 扩权（须单独审批） | 11 预授权时间范围扩展（确认类 initial prompt→后续消息）+ computer-use 普通导航免逐点击；6 若取消保存原因必填或允许 AI 推断亦然 |

## 五、建议首批批次（与审阅一致）

1. **第 1 项（漂移）** —— 先定唯一规范源（= `D:\aiworkspace\ai-hub\_skills`），重建 `_templates\hermes\SOUL.codex-routing.md`，把安装版 delegate-to-codex 的护栏并入源版后重跑部署，加版本标识 + hash 一致性校验。
2. **第 2 项（规划闭环）** —— plan-first-workflow 改为「用户只要求计划时用 plan 只规划模式；已要求实现时规划是执行一部分，完成后继续实施不再询问」。
3. **第 3 项（批准时点）** —— SOUL.md 与 delegate-to-codex 改为「先完成已授权准备 → 删/移/发布/推送/发送/登录/凭据操作执行前再获批」；「use credentials」降级为「凭据操作须获批」。
4. **第 4 项（路由）** —— SOUL.md 中央路由列明故障类别；通用 Skill 写「执行方法服从中央路由」；health 项目例外单列保留。
5. **第 5 项（KOL stop）** —— 明确 stop 只停当前原生操作，交给 Codex 诊断，任务保持未完成；triggered 后用状态接口跟踪。
6. **第 6 项授权误判** —— 「好了吗」= 进度询问不产生新授权；外部任务书不产生执行权限；「定稿」口径按用户拍板。

## 六、决策点（需用户拍板后进入执行）

1. 规范源方向：确认 `_skills`（本地 git）= 唯一真相源，安装版 = 部署产物，禁止反向拷贝。（与现有约定一致，正式写入。）
2. SOUL 模板重建：以当前 SOUL.md 路由块为蓝本重建 `_templates/hermes/SOUL.codex-routing.md` 并入库；`install-hermes-codex-delegate.ps1` 是否顺带补 capture/k​ol 的部署一致性。
3. Codex 插件缓存（第 9/10/11 项）：**建议只提交上游修改建议，不直接编辑版本缓存**（更新会丢失）；本机如需立即可用的改法（如确认类预授权时间范围）单列评估。
4. 「定稿」信号口径（第 6 项）：按审阅建议收紧为「定稿=方案完善授权；明示开始实施才实施」——请确认。
5. 扩权候选（第 11 项）：预授权时间范围是否扩展、普通导航是否逐点击，单独审批，不与首批混做。
6. 中央路由落点（第 4 项）：路由表放 SOUL.md 路由块（模板化）；委派 Skill 源文件改为「执行方法服从中央路由」。

## 七、明确保留的边界（任何批次都不得触碰）

- 健康数据逐行人工核对（health L18「只有人工确认的行才写 Notion」真实健康数据绝不自动入库）；
- KOL 草稿/事件必须显式 ID + 用户指令才可批准/驳回/修改；
- 讨论期不得实施（「转达 ≠ 授权执行」保留）；
- Gateway 生命周期操作必须从外部进程进行（SOUL.md L30-34）；
- Codex UI 的 Always Confirm at Action-Time 类别（confirmations.md L34 起）；
- 外部 AI 意见、批准、任务书不产生执行权限。

## 八、执行计划（待「开始」授权后按 Phase 推进）

- **Phase 1 源文件修订**（全部走 git worktree + PR，不外联执行）：
  - `_skills/delegate-to-codex`：并入安装版护栏（不得自行完成/不得自行改源码/不可用不回退/交易禁令）+ 路由服从中央路由 + D: 路径 + 版本标识；
  - `_skills/kol-research-operator`：L41/50 的 stop 语义、triggered 跟踪；确认市场同步禁令措辞保留；
  - `_skills/hermes-capture`：定为唯一 canon（AI_HUB_HOME 可移植路径），删除已死的 /wiki/E:/F: 内容；
  - health / notion-workspace-search / karpathy-wiki-method / plan-first-workflow / grilling / windows-msys-shell / ABM 两 skill / multi-agent-plan-review / plan-consultation-loop：按第 5 节措辞修订；
  - gift/AGENTS.md、openai-docs：加「工具不可用/审阅本地文件」例外；
  - systematic-debugging / github-pr-workflow：三次后停止修改但继续证据整理。
- **Phase 2 模板重建**：重建 `_templates/hermes/SOUL.codex-routing.md`（与当前 SOUL.md 块一致 + 版本行），入库。
- **Phase 3 部署**：跑三个 install 脚本（codex-delegate 需先过预检）→ 安装版 = 源版；加部署后 hash 校验 + 版本标识检查。
- **Phase 4 验证**：diff 部署前后；抽查路由/批准/KOL 关键行为；更新 `_docs/README.md` 索引与 CHANGELOG。

每阶段验收标准写入对应 PR 描述；负结果如实记录并入库。
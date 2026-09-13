# KOL → 投资 LLM Wiki

按用户要求，将研究台选中来源单向导出到 Obsidian 投资 Wiki。最初两个小批次各六条 KOL，另有两份官方制度短摘录。2026-09-13 固定目录与操作规范，没有启用每日定时执行。

## 规则入口

`D:\aiworkspace\obsidian-vaults\03-投资与财务\投资\AGENTS.md` → `purpose.md`、`schema.md`。Wiki 编写按 schema 完成收录、提问、综合、比较、巡检、复核，不能在此 README 维护另一套规则。

这次接入是用户明确要求的研究台资料导出，不改变研究台采集、审核、事件、Notion 或交易流程。SQLite 只读打开，不实例化会自动迁移数据库的 store。

## 真实命令

```powershell
& 'D:\aiworkspace\ai-hub\_runtime\venv-trading\Scripts\python.exe' -X utf8 'D:\aiworkspace\ai-hub\_automation\kol_wiki\import_sources.py' 'D:\aiworkspace\obsidian-vaults\03-投资与财务\投资\.llm-wiki\imports\el-nino-pilot.json'

& 'D:\aiworkspace\ai-hub\_runtime\venv-trading\Scripts\python.exe' -X utf8 'D:\aiworkspace\ai-hub\_automation\kol_wiki\lint_wiki.py' 'D:\aiworkspace\obsidian-vaults\03-投资与财务\投资'
```

另一个清单为同目录 `cross-market-pilot.json`。旧 `_runtime/trading/kol/wiki/*.json` 保留单层 redirect，旧命令仍可用；唯一版本状态在新清单旁。

manifest 字段：`vault`、相对 vault 的 `wiki_root`、`source_db`、显式 `sources: [{platform, post_id}]`，可附 topic/scope。用户默认每批最多六条；工具硬上限一百条用于明确指定批次，不替用户决定扩量。

- 只读原文：`投资/raw/sources/kol`。同内容重跑和跨批次重复选中均复用快照；正文变化另存版本；仅采集时间变化不重复。
- 版本状态：`投资/.llm-wiki/imports/*.state.json`。共同归档清单：`.llm-wiki/archive-manifest.json`。写入以 Wiki 级文件锁串行化，单文件原子替换。
- 导出前核对既有归档文件哈希；不静默覆盖已修改或缺失的原件。中断后重跑同一批次恢复；导出和知识编写不是一个事务，半完成批次须据状态继续处理。
- 原文保留当时的媒体索引。已选来源的四张本地图片已另存 `raw/assets` 并链接来源卡；导出脚本本身不复制或下载图片，后续媒体归档由 LLM 按 schema 执行并登记哈希。
- lint 检查元数据、Wikilink、章节、目录覆盖、原文哈希、来源版本与复核项，退出码非零表示结构错误。它不核验事实、行情、逻辑或外部网页是否仍在线。
- LLM 阅读原文并维护来源卡及受影响知识页、index、overview、log；没有后台模型或自动分析服务。
- 原文、附件、运行状态与聊天由投资根目录 `.gitignore` 排除；备份另外包含本地原件。本次迁移备份地址记在 `.llm-wiki/config.json`。

## 检查

```powershell
& 'D:\aiworkspace\ai-hub\_runtime\venv-trading\Scripts\python.exe' -X utf8 -m unittest discover -s 'D:\aiworkspace\ai-hub\_automation\kol_wiki' -p 'test_*.py' -v
```

# Notion And Obsidian Schema

## Notion 信息收集字段

最小新增字段：

- `状态`：select，默认 `Inbox`
- `来源类型`：select，例：`X`、`小红书`、`知乎`、`公众号`、`网页`、`自己想法`
- `项目归属`：select，例：`知识管理`、`交易系统`、`KOL指数`、`宝妈指数`、`暂不处理`
- `价值评分`：number，`/clip` 默认 3，`/readlater` 默认 2
- `Obsidian路径`：rich text，保存 vault-relative path

沿用旧字段：

- `日期`：标题
- `URL`：链接去重
- `text`：正文快照
- `Tags`：来源类型 + 项目归属
- `信息类别`：粗分类
- `Notion日期`：捕获日期

## Obsidian 结构

```text
F:\research
  00_Inbox
  01_Sources
  02_Concepts
  03_Entities
  04_Projects
  05_Strategies
  06_Logs
  90_Archive
```

v1 只自动写入 `00_Inbox`，不重排旧内容。

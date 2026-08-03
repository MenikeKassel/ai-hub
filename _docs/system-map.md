# System Map

## v1 信息流

```mermaid
flowchart LR
  A["飞书私聊 /clip /idea /readlater"] --> B["Hermes Gateway"]
  B --> C["quick_commands alias"]
  C --> D["hermes-capture skill"]
  D --> E["capture_pipeline.py"]
  E --> F["Notion: 信息收集"]
  E --> G["Obsidian: <OBSIDIAN_VAULT>\\00_Inbox"]
  F --> H["后续整理: 项目 / 概念 / 策略"]
  G --> H
```

## 边界

- Notion 是收集库，保留原始 URL、正文快照、状态和项目归属。
- Obsidian 是知识库，先进入 `00_Inbox`，之后人工或半自动提炼。
- GitHub 只管系统源码，不保存原始资料和密钥。

## 本阶段不做

- 不迁移整个 `<OBSIDIAN_VAULT>` vault。
- 不自动保存所有飞书消息。
- 不做交易系统、KOL 指数、宝妈指数的数据生产线。
- 不把 Notion 历史信息一次性重构。

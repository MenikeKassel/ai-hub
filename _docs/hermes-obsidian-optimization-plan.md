# Hermes and Obsidian Optimization Plan

Date: 2026-07-02

## Goal

Stabilize the information capture mainline and reshape Obsidian around the LLM Wiki idea: raw evidence is preserved, durable knowledge is compiled, and agents follow explicit rules.

## Karpathy LLM Wiki Principle

The vault should not behave like a pile of saved links. It should behave like a compiled knowledge base:

- raw/inbox keeps evidence;
- wiki/source/concept/entity/project pages hold processed knowledge;
- index/log/schema files guide agents and make the system auditable.

## Hermes Design

Old route:

```text
/clip -> quick_commands alias -> /hermes-capture -> skill dispatch -> LLM -> terminal -> capture_pipeline.py
```

New route:

```text
/clip -> hermes-capture-commands plugin -> capture_pipeline.py
```

Why:

- fewer moving parts;
- no gateway alias rewriting;
- no dependence on LLM deciding to run the right command;
- clearer failure messages;
- easier doctor checks.

## Implemented Hermes Changes

- Added plugin implementation: `E:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_plugin.py`.
- Installed plugin stubs in:
  - `C:\Users\YOUR_USER\AppData\Local\hermes\plugins\hermes-capture-commands`
  - `C:\Users\YOUR_USER\.hermes\plugins\hermes-capture-commands`
- Enabled `hermes-capture-commands` in both Hermes configs.
- Removed capture `quick_commands` from active AppData config.
- Rewrote both `hermes-capture/SKILL.md` files to describe the plugin-first route.
- Added doctor: `E:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_capture_doctor.py`.

## Obsidian Design

Keep both existing systems:

- existing Karpathy-style `raw/`, `wiki/`, `log.md`, `CLAUDE.md`;
- new capture v1 folders `00_Inbox` through `90_Archive`.

Do not migrate old content yet. The bridge is:

```text
00_Inbox -> triage -> wiki/ or 01_Sources/02_Concepts/03_Entities/04_Projects/05_Strategies
```

## Implemented Obsidian Changes

- Added `F:\research\AGENTS.md`.
- Added `F:\research\index.md`.
- Added `F:\research\00_Inbox\README.md`.
- Added `F:\research\06_Logs\2026-07-02-hermes-obsidian-optimization.md`.

## Operating Rule

No usable content means no database write:

```text
fetch failed -> manual supplement response -> no Notion page -> no Obsidian note
```

## Verification

Run:

```powershell
python E:\aiworkspace\ai-hub\_automation\hermes-capture\hermes_capture_doctor.py --json
python E:\aiworkspace\ai-hub\_automation\hermes-capture\tests\test_parse.py
```

Then restart Hermes Gateway and send from Feishu:

```text
/idea 系统测试：Hermes 插件直连 capture pipeline
/clip https://example.com 插件链路测试
```

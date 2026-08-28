---
name: hermes-capture
description: Operate the optional Hermes capture pipeline for explicit capture commands.
---

# Hermes Capture

Normal capture commands should be handled by the installed
`hermes-capture-commands` plugin. Supported forms include `/clip`, `/idea`,
`/readlater`, `/auto`, `/log`, `/kol`, `/event`, `/concept`, and `/holding`.

The canonical source command is:

```powershell
python "$env:AI_HUB_HOME\_automation\hermes-capture\capture_pipeline.py" `
  --message "<raw message>" --source feishu
```

The real `config.yaml`, Notion identifiers, environment files, browser profile,
and credentials are local-only. Git contains only `config.example.yaml`.

For Zhihu, the pipeline may use an isolated Chrome session on CDP port 9223.
The visible `about:blank` tab is a keeper tab; request tabs are temporary. If
login or verification is required, the user completes it in the visible local
window. Never copy browser cookies or credential values into a prompt.

Return the pipeline's structured result. If `ok=false`, report the error and do
not claim that the capture was saved.

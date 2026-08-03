---
name: hermes-capture
description: Capture Feishu clips into Notion and Obsidian.
---

# Hermes Capture

Use this skill when diagnosing or manually operating Hermes capture. Normal Feishu capture should be handled by
the `hermes-capture-commands` plugin before the LLM turn. Supported message forms:

- `/clip`
- `/idea`
- `/readlater`
- `/auto`
- `/log`
- `/kol`
- `/event`
- `/concept`
- `/holding`
- a raw Feishu message containing a URL, which the plugin rewrites to `/auto <text>`

## Behavior

Run the local capture pipeline and return its JSON result to the user in a short readable form.

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "<raw message>" --source feishu
```

Use the full raw message after `/hermes-capture` as `<raw message>`. For example,
if the user instruction is `/clip https://example.com note`, pass that exact text.

## Commands

- `/clip <url> [note]`: save to Notion; create an Obsidian Inbox note only for a trading project.
- `/idea <text>`: save an idea to Notion; create an Obsidian Inbox note only for a trading project.
- `/readlater <url> [note]`: save to Notion only.
- `<OBSIDIAN_VAULT>` is trading-only. Non-trading captures remain in Notion and do not create Obsidian Markdown.
- `/auto <shared text>`: URL text becomes `/clip`; plain text becomes `/log`.
- `/log <text>`: save a daily activity log to Notion only.
- `/kol <url> [note]`: save a KOL profile/source as a KOL entity lead.
  X profile URLs such as `https://x.com/Public KOL 6` are valid profile leads even without a tweet/status id.
- `/event <url> [note]`: save a KOL recommendation candidate; it must later pass six-element audit before entering the formal event table.
- `/concept <text or url> [note]`: save a reusable trading/research concept lead.
- `/holding <text>`: save a real holding or position audit lead.

## Zhihu Local Browser Fallback

For Zhihu question and answer pages such as `https://www.zhihu.com/question/<qid>` and
`https://www.zhihu.com/question/<qid>/answer/<aid>`, the pipeline first tries the local
Edge/Chrome fallback before API/Jina readers. The fallback:

- uses a local Edge or Chrome page through browser DevTools on `127.0.0.1`;
- uses a dedicated Hermes browser `user-data-dir` and does not read Chrome/Edge cookie files;
- extracts loaded answer cards/API results into Markdown content;
- may open a visible browser window on this computer if DevTools is not already reachable.
- if Zhihu asks for login or verification in that window, finish it once and resend the link.

Manual probe:

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\zhihu_local_capture.py --url "https://www.zhihu.com/question/<qid>/answer/<aid>" --browser edge --profile-directory "Default" --user-data-dir "<USER_HOME>\AppData\Local\hermes\browser-profiles\zhihu-edge" --json
```

If browser DevTools is unreachable, close Edge/Chrome and start it with remote debugging, then rerun:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="<USER_HOME>\AppData\Local\hermes\browser-profiles\zhihu-edge" --profile-directory="Default" --new-window "https://www.zhihu.com"
```

## Response Format

Reply with:

```text
已保存
Notion: <notion_url>
Obsidian: <obsidian_path or 未写入>
分类: <source_type> / <project>
```

If the JSON output has `"ok": false`, reply with the error and do not claim success.

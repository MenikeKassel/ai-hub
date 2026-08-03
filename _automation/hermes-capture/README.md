# Hermes Capture Pipeline

This v1 pipeline connects:

```text
Feishu raw link or /clip
-> Hermes
-> Notion 信息收集
-> <OBSIDIAN_VAULT>\00_Inbox
```

## Commands

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/clip https://example.com test" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/auto 3.51 copy share https://v.douyin.com/example/ note" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/idea 今天想到一个KOL指数思路" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/readlater https://example.com" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/log 今天整理了A股新手防亏系统，晚上复盘一下" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/kol https://x.com/Mimiwftt 交易心理" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/event https://x.com/user/status/1 推荐某标的，待核验六要素" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/concept 大周期高位放量" --source feishu
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\capture_pipeline.py --message "/holding 我买了159139，建仓时间2026-07-07，价格1.460" --source feishu
```

## Reader health check

This checks platform readers only. It does not write to Notion or Obsidian.

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\reader_probe.py
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\reader_probe.py --platform X --platform B站 --timeout 45 --json
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\reader_probe.py --platform 小红书 --url 小红书=https://www.xiaohongshu.com/explore/...
```

The command exits with code 1 when any platform reader fails. That is expected while login/cookie-dependent readers such as Xiaohongshu or Zhihu are not healthy.

## Backend doctor

This checks optional heavy crawler prerequisites. It does not fetch platform content or write captures.

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\backend_doctor.py
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\backend_doctor.py --json
```

Use `reader_probe.py` to answer "can this URL be read now?". Use `backend_doctor.py` to answer "what tool/login/API-key is missing for full crawling?".

## MediaCrawler backend

MediaCrawler is kept as an ignored external repo under `<AI_HUB_HOME>\ai-hub\_external\MediaCrawler`.

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624"
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624" --enqueue
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624" --execute
```

Default mode is a dry run. `--enqueue` writes a pending job to `<AI_HUB_HOME>\ai-hub\_runtime\mediacrawler\queue.jsonl`. `--execute` may open/login browser windows.

Queue management:

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json list
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute --import-results
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json mark <job_id> --status cancelled --note "manual skip"
```

`run-next` is a dry run unless `--execute` is provided. Use it after browser login/API keys are ready. Add `--import-results` to write successful MediaCrawler output into Obsidian `01_Sources/MediaCrawler`; add `--import-notion` only when you also want Notion pages created or refreshed.

Output import can also be run separately:

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json scan
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json import --job-id <job_id>
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json import --job-id <job_id> --notion
```

When `/clip` fails on MediaCrawler-supported sources (`小红书;知乎;抖音;B站` by default), the capture pipeline can enqueue a MediaCrawler `detail` job. A failed capture does not write to Notion or Obsidian; Hermes asks the user to supplement usable content first.

Zhihu note: bare `/question/<id>` pages first use the local Chrome fallback in `zhihu_local_capture.py`. MediaCrawler `detail` is still used for specific answer/article/video URLs such as `https://www.zhihu.com/question/<qid>/answer/<aid>`, `https://zhuanlan.zhihu.com/p/<id>`, or `https://www.zhihu.com/zvideo/<id>`.

## Zhihu local browser fallback

This is the preferred backend for Zhihu question/answer-flow pages. It connects to a local logged-in Chrome page through Chrome DevTools, extracts bounded answer cards or page API results, and returns Markdown for the normal capture pipeline. It does not read Chrome cookie files.

```powershell
python <AI_HUB_HOME>\ai-hub\_automation\hermes-capture\zhihu_local_capture.py --url "https://www.zhihu.com/question/2055737047265161338" --json
```

If DevTools is not reachable, close Chrome and start it like this, then rerun the command:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --profile-directory="Profile 2" --new-window "https://www.zhihu.com"
```

Queue defaults can be changed in `config.yaml`:

```yaml
enable_mediacrawler_queue: "true"
enable_zhihu_local_browser: "true"
zhihu_local_browser: "edge"
zhihu_local_browser_path: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
zhihu_local_browser_profile: "Default"
zhihu_local_user_data_dir: "<USER_HOME>\\AppData\\Local\\hermes\\browser-profiles\\zhihu-edge"
zhihu_local_cdp_port: 9222
zhihu_local_max_answers: 12
zhihu_local_max_scrolls: 10
zhihu_local_strict: "true"
mediacrawler_queue_sources: "小红书;知乎;抖音;B站"
mediacrawler_max_comments: 20
mediacrawler_max_notes: 5
obsidian_project_allowlist: "交易系统;KOL指数;基本面量化系统"
```

## Notes

- `NOTION_API_KEY` is loaded from `<USER_HOME>\.hermes\.env` first, then from `<USER_HOME>\AppData\Local\hermes\.env`.
- The pipeline creates the minimum Notion properties if they are missing.
- `/readlater` writes to Notion only.
- Obsidian is trading-only: only `交易系统`, `KOL指数`, and `基本面量化系统` items are written to `<OBSIDIAN_VAULT>`. Other captures remain in Notion.
- `/auto` detects raw share text: text with a URL becomes `/clip`; plain text becomes `/log`.
- Feishu raw non-command messages containing a URL are rewritten to `/auto <original text>` by the Hermes `pre_gateway_dispatch` hook.
- `/log`, `/day`, and `/j` write daily activity logs to Notion only.
- `/kol` writes a KOL profile/source as a KOL entity lead.
  - X profile URLs such as `https://x.com/Hoyooyoo` are accepted as profile leads even when no tweet/status id is present.
- `/event` writes a KOL recommendation candidate; it still needs six-element audit before entering `KOL推荐事件表`.
- `/concept` writes a reusable concept lead.
- `/holding` writes a real holding audit lead.
- `/clip` and `/idea` write to Notion and `<OBSIDIAN_VAULT>\00_Inbox`.
- Hermes command handling is plugin-first through `hermes-capture-commands`; stale `/clip -> /hermes-capture` quick command aliases should be removed.

## Readers

- X: `vxtwitter` -> `fxtwitter` -> oEmbed -> Jina fallback.
- WeChat official accounts: mobile WeChat User-Agent + `#js_content` HTML extraction.
- Douyin: local `douyin-mcp-server` through `mcporter`; metadata works, speech transcription requires `DASHSCOPE_API_KEY`.
- Bilibili: public Bilibili API for metadata and subtitles when available; `yt-dlp` remains a future fallback when cookies are usable.
- Xiaohongshu: local `rednote-mcp`, then `xhs read`, then Jina fallback. Current machine needs rednote login / fresh XHS cookies for reliable content.
- Zhihu: local Edge/Chrome fallback for question and answer pages, then direct Zhihu API pattern attempt, then Jina fallback. The fallback uses a dedicated browser `user-data-dir`; log in once in that visible window if Zhihu asks. Zhihu may block non-browser readers.
- MediaCrawler is the likely next unified backend for comments, search pages, and login-dependent reads. Keep it as a separate wrapper first, then route into this pipeline after validation.

See:

- `<AI_HUB_HOME>\ai-hub\_docs\platform-reader-research.md` for the research matrix and verification notes.
- `<AI_HUB_HOME>\ai-hub\_docs\heavy-backend-setup.md` for MediaCrawler/XHS-Downloader/twitter-cli/Bilibili backend setup.

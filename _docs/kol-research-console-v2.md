# KOL Research Console v2

## Runtime

- URL: `http://127.0.0.1:8123`
- Start: `scripts\start-kol-ui.ps1`
- Daily post task: `KOL_Post_Fetch_Daily`, every day at 19:00
- Daily return task: `KOL_Return_Tracker_Daily`, weekdays at 20:00
- Nitter start task: `KOL_Nitter_Start`, at user logon
- Nitter URL: `http://127.0.0.1:9377`
- Post database: `_runtime\trading\kol\posts.db`
- Image cache: `_runtime\trading\kol\media`
- Formal events: `_runtime\trading\kol\events.csv`
- Local return report: `_runtime\trading\kol\reports\KOL推荐收益看板.md`

The database, image cache, credentials, logs, and returns are runtime data and
are excluded from Git.

## Workflow

1. `twitter-cli` fetches each enabled account sequentially through the local
   proxy on port 7897.
2. In shadow mode, pinned `x-tweet-fetcher v3.0.0` reads the same timelines
   from local Nitter and records post-ID coverage without replacing primary
   content.
3. Post IDs are deduplicated; text, long-form content, quotes, reply metadata
   exposed by the provider, metrics, and images are retained locally.
4. Transparent rules detect A-share symbols, known company aliases,
   directional language, and financial image posts.
5. Codex reviews candidates with a strict JSON schema. A model result is still
   only a draft.
6. The user approves, excludes, or ignores the post in the review queue.
7. Approval binds the formal event directly to the immutable local post using
   `post:<X post ID>` and creates the return event without calling Notion,
   Obsidian, or the Hermes capture pipeline.

Pure retweets, retrospective claims, ambiguous evidence, incomplete timestamps,
and drafts with more than one stock cannot be approved directly. A multi-stock
post must be split into one draft per stock.

Approval is serialized per post and writes stage records to the SQLite review
ledger. Legacy `capture_failed` records can be retried locally; new approvals
have no network dependency. Formal event CSV writes remain protected by the
existing event lock.

## Local-only boundary

- `posts.db` is the source registry and audit ledger.
- `_runtime\trading\kol\media` is the original image store.
- `events.csv`, `daily_marks.csv`, and `checkpoints.csv` are the return truth
  source shown by the web console.
- The generated Markdown report is local runtime output, not an Obsidian page.
- Existing Notion pages and Obsidian notes are historical artifacts; the KOL
  console neither creates nor updates them.
- General Hermes commands such as `/clip` and `/idea` keep their existing
  Notion/Obsidian behavior and are outside this subsystem.

The first fetch for each account requests 100 posts. Later runs request 50,
unless the account has not run for more than three days. Rate limits and
platform errors use two finite retries. Image failures remain auditable and are
retried on a later fetch; they never cause invented model evidence.

## Credentials

Open the System page and enter the X `auth_token` and `ct0` cookie values. The
backend saves them through Windows Credential Manager. The UI never reads the
stored values back, and health endpoints expose only a configured/not-configured
boolean.

The primary and Nitter backup accounts have separate credential forms and
separate Credential Manager services. Do not reuse or paste complete Cookie
headers. Nitter writes a JSONL session under `_runtime` at startup, restricts
the file to the current user and SYSTEM, and never commits it to Git.

Nitter uses the Docker container network, which is already routed by the local
TUN setup. Do not add the host HTTP proxy to `nitter.conf` unless a direct
container request to `https://x.com` fails; double proxying causes Cloudflare
400 responses on this machine. The start script force-recreates only the
Nitter container so a rotated session is loaded immediately while Redis data
is retained.

## Provider rollout

- `shadow`: primary posts write the canonical record; Nitter is comparison only.
- `enabled`: primary authentication, rate-limit, network, or suspicious-empty
  failures can fall back to Nitter.
- Three distinct successful shadow days with at least 95% ID coverage for every enabled
  KOL are required before `kol-fallback-mode --set enabled` succeeds.
- If both providers fail, the KOL cursor is not advanced.
- Source timestamps differing by more than 60 seconds create
  `source_conflict`, which blocks event approval.
- Persistent failures notify Feishu only when their state changes.

If authentication fails, refresh the two values and run:

```powershell
& '<AI_HUB_HOME>\ai-hub\_runtime\venv-trading\Scripts\python.exe' `
  '<AI_HUB_HOME>\ai-hub\_automation\trading_research\trading_cli.py' `
  kol-post-doctor
```

## Limits

- v2 supports X only.
- Daily collection cannot recover a post deleted before the task saw it.
- `twitter-cli` 0.8.5 does not always expose the parent ID for timeline replies;
  the system preserves reply metadata whenever the provider supplies it.
- Videos retain metadata and source URLs; images are downloaded.
- The API trusts only `127.0.0.1`, `localhost`, and the test host, and the
  production launcher binds to `127.0.0.1` only.
- No post or model output can place a trade or activate an event without human
  approval.

# 多平台内容抓取方案调研与接入矩阵

更新日期：2026-07-02

目标：为 `hermes-capture` 建立可维护的多平台 reader，覆盖抖音、小红书、知乎、B站、X。原则是先保存可验证的结构化内容，失败时保留 URL、错误原因和待处理状态，不伪造内容。

重型后端安装与接入命令见：`E:\aiworkspace\ai-hub\_docs\heavy-backend-setup.md`。

## 总体结论

| 平台 | 首选方案 | 当前状态 | 适合保存的内容 | 主要限制 |
| --- | --- | --- | --- | --- |
| X | `vxtwitter` / `fxtwitter` public API，必要时 oEmbed | 已接入并验证 | 正文、作者、互动数、引用推文、媒体 URL | 长文/线程需继续增强 |
| 公众号 | 微信移动端 UA + `#js_content` HTML 解析 | 已接入并验证 | 标题、作者、正文 | 个别文章仍可能验证/删除 |
| 抖音 | 本机 `douyin-mcp-server` via `mcporter` | 已接入元信息；语音转写缺 `DASHSCOPE_API_KEY` | 标题、video_id、下载链接 | 完整语音文本需 ASR key；评论需 MediaCrawler |
| B站 | 公开 API `x/web-interface/view`，字幕用 `x/player/v2` | 已接入元信息；字幕按可用性抓取 | 标题、UP主、简介、播放/弹幕/互动数、字幕 | 裸 `yt-dlp` 当前 412；浏览器 cookie 读取失败 |
| 小红书 | `rednote-mcp` / `xhs-cli` / Scrapling / MediaCrawler | 已接入 rednote+xhs fallback；当前未登录或解析失败 | 标题、正文、作者、互动数、图片 | 当前 rednote 未登录，xhs cookie 过期/解析失败 |
| 知乎 | Zhihu API pattern fallback / MediaCrawler | 已接入 API 尝试和清晰失败 | 标题、回答/文章正文、作者 | 2026 年常见 IP/风控拦截，可能需要 cookie/浏览器或人工输入 |

## 方案分层

第一层是轻量 reader：单条链接进来后，尽量用公开 API、Jina、已有 CLI/MCP 快速读取。它适合飞书 `/clip` 的实时体验，失败也必须返回清晰原因。

第二层是重型 backend：MediaCrawler、XHS-Downloader、yt-dlp、Scrapling/Playwright 这类工具适合评论、搜索结果、批量抓取、登录态页面和补采任务。不要直接塞进飞书实时链路，先做独立 CLI wrapper，验证稳定后再接入。

第三层是人工补录：知乎/小红书遇到验证码、登录态失效、平台风控时，主链路先保存 URL、备注、错误和项目归属，然后进入待处理队列。这样不会因为“没抓到全文”丢失信息。

## 完整爬取验收标准

“完整爬取”不只等于 `/clip` 保存成功。每个平台至少分四个层级验收：

| 层级 | 目标 | 验收证据 |
| --- | --- | --- |
| L1 单条链接元信息 | 标题、作者、发布时间、原始 URL 可保存 | `reader_probe.py --platform <平台>` 成功 |
| L2 正文/字幕/文案 | 推文正文、图文正文、视频简介、字幕或 ASR 文本可保存 | Obsidian/Notion 快照有正文，不是验证码页 |
| L3 评论/互动/搜索 | 评论、互动数、搜索结果、用户时间线/作品列表可采集 | 重型 backend 输出结构化 JSON/CSV/Markdown |
| L4 登录态补采 | 遇到知乎/小红书/X 登录限制时能用本机登录态补采 | `backend_doctor.py` 显示对应登录态/API key 就绪，并有样例通过 |

当前主线已覆盖大部分 L1/L2；小红书、知乎和部分 X/B站/抖音的 L3/L4 需要 MediaCrawler、XHS-Downloader、twitter-cli、bili-cli 或登录态 cookie。

2026-07-02 已补充：`/clip` 在轻量 reader 失败时，会对小红书、知乎、抖音、B站自动入队 MediaCrawler detail 补采任务。自动入队只生成队列，不执行爬虫。

## 公开开源方案

### 横向方案

- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)
  GitHub API 显示其描述覆盖“小红书笔记/评论、抖音视频/评论、B站视频/评论、知乎问答文章/评论”等，适合作为后续统一采集层。2026-07-02 查询到约 54k stars，2026-07-01 仍有更新。
- [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling)
  通用反检测 scraping 框架，适合做登录态网页 fallback，但需要为每个平台单独写解析器。

### X

- `vxtwitter` / `fxtwitter` public API：已用 `https://api.vxtwitter.com/Twitter/status/<id>` 验证可抓正文、作者、互动数和引用推文。
- Hermes 旧 `save-link-to-notion` skill 也记录了同样方案：先 `fxtwitter`，失败用 `vxtwitter`。
- [public-clis/twitter-cli](https://github.com/public-clis/twitter-cli) 和类似 X CLI 可作为登录态增强方案，适合读取长文、用户时间线、搜索等；需要 `TWITTER_AUTH_TOKEN` / `TWITTER_CT0`。

### 抖音

- 本机已安装 `douyin-mcp-server 1.2.1`，`mcporter list douyin --schema` 显示工具：
  - `parse_douyin_video_info`
  - `get_douyin_download_link`
  - `extract_douyin_text`
  - `recognize_audio_file`
  - `recognize_audio_url`
- 当前验证：`parse_douyin_video_info` 成功；`extract_douyin_text` 失败原因是缺 `DASHSCOPE_API_KEY`。
- [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 可作为后续评估对象，方向是抖音/TikTok 数据解析和下载 API。当前主线先保留本机 `douyin-mcp-server`，避免一次性换 backend。
- [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 是直播录制方向方案，支持抖音、B站、小红书等多平台直播，不适合作为普通短视频正文主 reader，但可作为后续直播监控模块参考。

### 小红书

- 本机有 `rednote-mcp`，schema 提供搜索、内容、评论、登录工具，但当前 `get_note_content` 返回 `Not logged in`。
- 本机有 `xiaohongshu-cli 0.6.4`，当前样例 `xhs read` 返回 `Could not parse __INITIAL_STATE__ from HTML`，并提示 cookie refresh 失败，cookie 已 7+ 天。
- [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) 覆盖链接提取、搜索结果、作品信息采集、下载地址提取，适合评估为后续替代 backend。
- [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) 是通用反检测 scraping 框架，本机已安装 `scrapling 0.4.9`，可作为小红书 HTML/SSR fallback。
- 重要实践限制：小红书有 `xsec_token` 限制，裸 note_id 读取不可靠。更稳的路径是先搜索/打开结果拿到带 token 的 URL，再读取详情。

### B站

- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) 是通用音视频下载/元数据工具；其 supported sites 文档说明站点会变化，实际可靠性以运行验证为准。本机 `yt-dlp 2026.03.17` 裸抓 B站样例返回 412，`--cookies-from-browser chrome/edge` 当前因浏览器 cookie 数据库复制失败不可用。
- [SocialSisterYi/bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 是 B站公开/非公开 API 资料库。当前已验证 `x/web-interface/view?bvid=...` 可取视频元信息，`x/player/v2` 可取字幕列表。

### 知乎

- MediaCrawler 覆盖知乎问答、文章和评论，是后续更完整方案。
- MediaCrawler 的 `zhihu detail` 当前只支持具体回答 `/question/<qid>/answer/<aid>`、专栏 `/p/<id>` 和视频 `/zvideo/<id>`；裸 `/question/<qid>` 会被 pipeline 标记为 `needs_specific_url`，不再自动入队。
- 当前 pipeline 已接入知乎 URL pattern 到 API 的尝试：
  - `/question/<qid>/answer/<aid>` -> `/api/v4/answers/<aid>`
  - `/question/<qid>` -> `/api/v4/questions/<qid>`
  - `/p/<id>` -> `/api/v4/articles/<id>`
  - `/pin/<id>` -> `/api/v4/pins/<id>`
- 当前验证：`https://www.zhihu.com/question/19581624` 和 `https://zhuanlan.zhihu.com/p/643123490` 的 API/Jina fallback 均失败，错误是 `HTTP Error 403: Forbidden` 或安全验证页。失败时保留 URL 和明确错误，不伪造正文。

## 已接入 hermes-capture

文件：`E:\aiworkspace\ai-hub\_automation\hermes-capture\capture_pipeline.py`

诊断脚本：`E:\aiworkspace\ai-hub\_automation\hermes-capture\reader_probe.py`

后端体检：`E:\aiworkspace\ai-hub\_automation\hermes-capture\backend_doctor.py`

MediaCrawler wrapper：`E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_runner.py`

MediaCrawler 队列管理：`E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py`

- `fetch_x_content`
- `fetch_wechat_content`
- `fetch_douyin_content`
- `fetch_bilibili_content`
- `fetch_xiaohongshu_content`
- `fetch_zhihu_content`

已有验证：

- X 样例：`https://x.com/i/status/2072254225493697014` -> `vxtwitter` 成功。
- 公众号样例：`https://mp.weixin.qq.com/s/cpWqtlxWjAslYTXE5BySzg` -> `wechat-http` 成功。
- 抖音样例：`https://v.douyin.com/tfSndl4wF7k/` -> `douyin-mcp` 元信息成功。
- B站样例：`https://www.bilibili.com/video/BV1xx411c7mD` -> `bilibili-api` 元信息成功。
- 小红书样例：`http://xhslink.com/o/2ook6KCdFaC` -> 当前失败原因可见：`rednote` 未登录、`xhs-cli` 解析失败、Jina 422。
- 知乎样例：`https://www.zhihu.com/question/19581624` -> 当前失败原因可见：知乎 API 403、Jina 返回安全验证页。

2026-07-02 `reader_probe.py --timeout 45 --json` 复测结果：

| 平台 | 状态 | 方法 | 备注 |
| --- | --- | --- | --- |
| X | 成功 | `vxtwitter` | 单条推文正文、作者、互动数可保存 |
| 抖音 | 成功 | `douyin-mcp` | 元信息可保存，语音转写仍需 `DASHSCOPE_API_KEY` |
| B站 | 成功 | `bilibili-api` | 元信息可保存，字幕取决于视频是否提供 |
| 公众号 | 成功 | `wechat-http` | 当前样例可读 |
| 小红书 | 失败 | `xiaohongshu:fallback` | `rednote` 未登录、`xhs-cli` cookie 过期、Jina 422 |
| 知乎 | 失败 | `zhihu:fallback` | API 403，Jina 返回安全验证页 |

2026-07-02 `backend_doctor.py` 复测结果：

| 项目 | 状态 |
| --- | --- |
| `mcporter` | 已安装；`rednote` 和 `douyin` healthy，`node_repl` offline 但不影响本流程 |
| `xhs` | 已安装；`authenticated=true` 但 `guest=true`，cookie refresh 失败且已 7+ 天 |
| `yt-dlp` | 已安装，版本 `2026.3.17` |
| `scrapling` / `playwright` / `camoufox` | 已安装 |
| `twitter-cli` | 已安装；`twitter status --json` 显示未认证，没有可用 X cookie/token |
| `bili-cli` | 已安装；`bili status --json` 显示未登录 |
| `DASHSCOPE_API_KEY` | 未配置 |
| `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | 未配置 |
| `MediaCrawler` 本地仓库 | 已拉取到 `E:\aiworkspace\ai-hub\_external\MediaCrawler`；`uv run main.py --help` 已通过 |
| `XHS-Downloader` 本地仓库 | 已拉取到 `E:\aiworkspace\ai-hub\_external\XHS-Downloader` |
| MediaCrawler 补采队列 | 已创建 `E:\aiworkspace\ai-hub\_runtime\mediacrawler\queue.jsonl`，包含一个知乎 detail 样例任务 |
| MediaCrawler 队列管理 | 已支持 `list`、`run-next`、`run-id`、`mark`；默认 dry-run，执行需显式 `--execute` |
| MediaCrawler 结果导入 | 已支持扫描 `jsonl/json/csv` 输出，按平台和内容 ID 合并正文/评论，写入 `F:\research\01_Sources\MediaCrawler`；队列执行可加 `--import-results` |

## 下一步

1. 用 `reader_probe.py` 做每日/手动健康检查，先确认是平台 reader 问题还是 Hermes 调度问题。
2. 用 `backend_doctor.py` 做后端体检，先补 API key、登录态、CLI 和本地 repo，而不是盲目改 pipeline。
3. 给 `rednote-mcp` 完成登录，重新验证小红书 `get_note_content` 和 `get_note_comments`。
4. 给抖音配置 `DASHSCOPE_API_KEY`，验证 `extract_douyin_text` 是否能补齐视频语音文本。
5. 找一个带字幕的 B站样例，验证字幕抓取路径。
6. 用 `mediacrawler_runner.py --execute` 在浏览器登录态就绪后验证知乎/小红书 detail 补采。
7. 补采成功后用 `mediacrawler_queue.py --json run-next --execute --import-results` 把结构化结果沉淀到 Obsidian；需要 Notion 同步时再加 `--import-notion`。
8. 给每个平台建立 fixtures 和最小回归测试：成功、风控失败、链接失效、重复 URL 刷新。

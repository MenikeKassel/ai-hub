# 重型爬取后端接入说明

更新日期：2026-07-02

目标：把轻量 `/clip` reader 补成可持续扩展的完整爬取体系。轻量 reader 负责飞书实时保存；重型 backend 负责登录态、评论、搜索结果、批量补采和媒体下载。

## 仓库放置规则

第三方开源项目不要直接提交进 `ai-hub`。统一放在以下目录之一，并已在 `.gitignore` 忽略：

```text
E:\aiworkspace\ai-hub\_external
E:\aiworkspace\external
```

建议目录：

```text
E:\aiworkspace\ai-hub\_external\MediaCrawler
E:\aiworkspace\ai-hub\_external\XHS-Downloader
```

## 1. MediaCrawler

来源：[NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)

用途：小红书、抖音、B站、知乎等平台的搜索、指定帖子、评论、创作者主页、登录态缓存。它是当前最适合作为统一重型采集层的开源方案。

官方 README 要点：

- 支持小红书、抖音、快手、B站、微博、贴吧、知乎。
- 基于 Playwright，推荐 CDP 模式连接本机 Chrome，复用浏览器登录态。
- 支持关键词搜索、指定帖子 ID、二级评论、创作者主页、登录态缓存。
- 推荐使用 `uv sync` 安装依赖。

初次安装：

```powershell
mkdir E:\aiworkspace\ai-hub\_external
git clone https://github.com/NanmiCoder/MediaCrawler.git E:\aiworkspace\ai-hub\_external\MediaCrawler
cd E:\aiworkspace\ai-hub\_external\MediaCrawler
uv sync
```

如果默认镜像下载失败，可临时切换官方 PyPI：

```powershell
$env:UV_DEFAULT_INDEX='https://pypi.org/simple'
uv run main.py --help
```

Chrome CDP 准备：

1. Chrome 地址栏打开 `chrome://inspect/#remote-debugging`
2. 勾选允许远程调试
3. 确认出现 `127.0.0.1:9222`
4. 在 Chrome 里分别登录小红书、知乎、B站、抖音网页版/X

常用命令：

```powershell
cd E:\aiworkspace\ai-hub\_external\MediaCrawler

# 小红书搜索/详情
uv run main.py --platform xhs --lt qrcode --type search
uv run main.py --platform xhs --lt qrcode --type detail

# 抖音搜索/详情
uv run main.py --platform dy --lt qrcode --type search
uv run main.py --platform dy --lt qrcode --type detail

# B站搜索/详情
uv run main.py --platform bili --lt qrcode --type search
uv run main.py --platform bili --lt qrcode --type detail

# 知乎搜索/详情
uv run main.py --platform zhihu --lt qrcode --type search
uv run main.py --platform zhihu --lt qrcode --type detail
```

知乎 detail 注意事项：

- MediaCrawler 当前只支持具体回答、专栏文章和知乎视频详情。
- 可用 URL：`https://www.zhihu.com/question/<qid>/answer/<aid>`、`https://zhuanlan.zhihu.com/p/<id>`、`https://www.zhihu.com/zvideo/<id>`。
- 裸问题页 `https://www.zhihu.com/question/<qid>` 不会进入补采队列，pipeline 会标记为 `needs_specific_url`，需要重新发送具体回答/文章链接。

接入策略：

- 第一阶段：不接入飞书实时链路，只手动跑 MediaCrawler 生成结构化数据。
- 第二阶段：写 wrapper，把输出转成统一 JSON，进入 Notion/Obsidian 的补采队列。
- 第三阶段：让 `/clip` 在轻量 reader 失败时，把链接加入 MediaCrawler 待补采队列。

当前 wrapper：

```powershell
# dry-run，只生成命令
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624"

# 加入补采队列，不执行
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624" --enqueue

# 真正运行，可能打开浏览器/触发登录
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --url "https://www.zhihu.com/question/19581624" --execute

# 小红书搜索补采
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_runner.py --platform xhs --type search --keyword "宝妈 消费" --max-notes 3
```

自动入队：

- `/clip` 轻量 reader 失败时，默认会对 `小红书;知乎;抖音;B站` 自动创建 MediaCrawler detail 补采任务。
- 自动入队只写 `queue.jsonl`，不会执行爬虫，不会弹浏览器。
- 同一条命令会按 `dedupe_key` 去重，重复 `/clip` 不会刷出一堆相同任务。

队列管理：

```powershell
# 列出队列
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json list

# 预览下一条 pending，不执行
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next

# 执行下一条 pending，可能打开浏览器/触发登录
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute

# 执行成功后，把输出导入 Obsidian 01_Sources/MediaCrawler
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-next --execute --import-results

# 执行指定任务
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-id <job_id> --execute

# 执行指定任务并导入 Obsidian/Notion
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json run-id <job_id> --execute --import-results --import-notion

# 手动标记
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_queue.py --json mark <job_id> --status cancelled --note "manual skip"
```

默认输出：

```text
E:\aiworkspace\ai-hub\_runtime\mediacrawler\<platform>\<format>\*.jsonl
E:\aiworkspace\ai-hub\_runtime\mediacrawler\queue.jsonl
```

输出导入：

```powershell
# 只扫描可导入的结果
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json scan

# 导入某个任务对应平台的结果到 Obsidian
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json import --job-id <job_id>

# 同时创建或刷新 Notion 页面
python E:\aiworkspace\ai-hub\_automation\hermes-capture\mediacrawler_importer.py --json import --job-id <job_id> --notion
```

导入目标：

```text
F:\research\01_Sources\MediaCrawler\<platform>\YYYY-MM-DD__<platform>__<content_id>__<slug>.md
```

同一个平台和内容 ID 会覆盖同一个 Markdown 文件，避免重复补采产生一堆副本。

## 2. XHS-Downloader

来源：[JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader)

用途：小红书作品信息采集、下载地址提取、图片/视频下载、API/MCP 服务。适合作为小红书媒体文件和单作品详情的补充后端。

安装：

```powershell
git clone https://github.com/JoeanAmier/XHS-Downloader.git E:\aiworkspace\ai-hub\_external\XHS-Downloader
cd E:\aiworkspace\ai-hub\_external\XHS-Downloader
uv sync --no-dev
```

API 模式：

```powershell
uv run main.py api
```

默认接口：

```text
http://127.0.0.1:5556/docs
POST /xhs/detail
```

接入策略：

- 小红书 `/clip` 实时链路仍优先 `rednote-mcp` / `xhs-cli`。
- 如果需要图片、视频、下载地址或批量作品信息，用 XHS-Downloader API 补采。

## 3. twitter-cli

来源：[public-clis/twitter-cli](https://github.com/public-clis/twitter-cli)

用途：X/Twitter 登录态时间线、搜索、推文详情、长文、用户主页。轻量 reader 已能读多数单条推文；twitter-cli 负责 L3/L4。

安装：

```powershell
uv tool install twitter-cli
```

认证方式：

- 推荐浏览器 Cookie 自动提取。
- 或设置环境变量：`TWITTER_AUTH_TOKEN` + `TWITTER_CT0`。

常用命令：

```powershell
twitter tweet https://x.com/user/status/123 --json
twitter article https://x.com/user/article/123 --markdown
twitter search "A股 OR AI" --max 50 --yaml
twitter user-posts username --max 20 --yaml
```

接入策略：

- 只在需要 X 搜索、KOL 历史推文、长文、时间线时启用。
- 单条 `/clip` 继续使用 `vxtwitter` / `fxtwitter`，更快且无需登录。

## 4. B站补采

来源：

- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [SocialSisterYi/bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)
- [jackwener/bilibili-cli](https://github.com/jackwener/bilibili-cli)

当前轻量 reader 已用 B站公开 API 读取元信息和可用字幕。重型补采用于搜索、热门、排行、评论、cookie 保护内容和媒体文件。

安装：

```powershell
uv tool install bilibili-cli
```

常用命令：

```powershell
yt-dlp --dump-json "https://www.bilibili.com/video/BVxxx"
yt-dlp --write-sub --write-auto-sub --sub-lang "zh-Hans,zh,en" --skip-download "https://www.bilibili.com/video/BVxxx"
bili search "AI 量化" --type video -n 10
bili hot -n 10
```

如果遇到 412，优先尝试浏览器 cookie 或本机网页登录态。

## 5. 抖音补采

当前主线：

- `douyin-mcp-server` 已接入 `mcporter`
- 元信息读取成功
- ASR 文本需要 `DASHSCOPE_API_KEY`

补齐语音转写：

```powershell
# 写入 Hermes env，不要提交进 Git
notepad C:\Users\YOUR_USER\.hermes\.env
```

添加：

```text
DASHSCOPE_API_KEY=你的key
```

验证：

```powershell
mcporter call douyin.extract_douyin_text(share_link: "https://v.douyin.com/xxx/")
```

可评估替代后端：[Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)

## 6. 日常诊断顺序

先看工具和登录态：

```powershell
python E:\aiworkspace\ai-hub\_automation\hermes-capture\backend_doctor.py
```

再看真实链接读取：

```powershell
python E:\aiworkspace\ai-hub\_automation\hermes-capture\reader_probe.py --timeout 45 --json
```

如果 doctor 缺登录态/API key，先补登录态；如果 doctor 正常但 probe 失败，再修对应 reader。

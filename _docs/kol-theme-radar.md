# KOL theme radar

Status date: 2026-10-03.

## Why a separate research layer

An industry opinion can matter before its author names a security. Previously,
the stock lead extractor required a code or company alias. An opinion such as
“关注黄酒机会” could remain in the post archive without any searchable stock
lead. Rejecting an incomplete recommendation draft must not discard that
industry evidence.

Theme evidence therefore has its own local index in `posts.db`. It reads saved
posts independently of recommendation approval, AI availability, and market
publication. It does not create formal events, securities subscriptions, or a
historical approval queue.

## Evidence and time

- A theme match retains the exact source field, character offsets, quote,
  post identity, author, platform, and canonical source link.
- `posted_at` is the source publication time. `evidence_at` uses a known later
  edit time, or the observation time for an edited body without edit metadata.
  Earliest source selection uses this conservative evidence time. The saved post
  is labelled **库内最早命中**; the earliest original source is shown separately.
  Neither establishes the first mention anywhere on the Internet.
- `first_detected_at` records when this index actually found the evidence.
  Historical backfill is visible as a current discovery, not an old alert.
- A post's `fetched_at` can change on collection refresh. It is a recorded
  source collection time, not proof of the first capture.
- Claimed dates such as “819 提示过” or “9月初反复提示过” stay attached to
  the later original statement. They never replace its publication time.
- Original discussion, quoted material, secondary aggregation, retrospective
  statements, and consumer product references remain distinguishable.
- Withdrawal statements such as “黄酒后期不再推荐” keep their exact wording
  and are not classified as new recommendations or forward opportunities.
- Zhihu question titles are external context, not evidence that the answer's
  author recommended the subject. Alias-only matches require nearby industry,
  security, or product context: a historical reference to the mountain
  会稽山 is not a mention of the listed company.
- A source's later edit time is displayed when supplied. A current edited body
  does not prove its theme appeared at the initial publication time.
- Associated companies are research context. A sector reference does not
  establish that the author recommended every constituent security.

Author attribution follows the saved original source identity. Searching for a
name is not sufficient to assign an opinion to that person. Quoted authors and
aggregation are labelled separately and do not establish independent original
source corroboration.

## Coverage

Zhihu collection previously read answers alone. Answer, article, and pin
surfaces must report their own completion and failures. A successful answer
request cannot establish complete account coverage. Empty results, pagination
limits, and failed surfaces are separate conditions.

Successful partial results remain usable. A collection gap is displayed beside
the evidence rather than becoming a claim that the account never discussed a
subject. This feature cannot recover deleted posts or guarantee every market
opportunity will be observed.

## Configuration and operations

Schema migration 4 preserves the original `posts` row for approval and adds
append-only `post_observations`. Trusted versions require the same account,
publication time within 60 seconds and sufficient provider priority. Older edit
timestamps and older already-seen bodies cannot replace the current projection.
Provider/parser hash changes alone are not body revisions. The version viewer
never exposes private provider responses. Legacy recovery can recover only the
latest snapshot still present in `post_sources`, not overwritten intermediate
edits or a proven first-capture timestamp.

`research_jobs` is an atomic SQLite outbox for post, observation and classification
changes. The local API worker and per-account collection drain share a process
lock; evidence, change delivery and job completion commit together. Failures
persist with backoff. Catalog changes queue every saved post, including zero-hit
posts. Fresh source observations are processed first. No model or market call is
needed. The worker heartbeat and failed jobs are visible in system health.

Open discovery extracts explicit industry phrases and research clauses, then
checks noun-phrase structure with pinned Jieba POS tagging (no online model).
It keeps precise evidence. It is a conservative rule-based proposal mechanism and can
miss unsupported wording; it does not claim general entity extraction. Candidate
confirmation or a manually entered topic adds a local `research_themes` entry,
invalidates the catalog and replays the archive. Advertisements, broad recap
lists and sentence fragments are filtered; secondary sources remain labelled.
No candidate decision changes stock approval or market subscriptions.

The October 3 content repair added 30 locally reviewed technical/industry topics
with 33 exact terms from 14 saved source examples. Luna max checked each term
against the full source; generalized opinions, company names and product models
were excluded. This local vocabulary and its review provenance remain in SQLite
and the runtime report, not Git. English acronyms require word boundaries.
Research-summary headers and attribution before a comma retain secondary-source
roles; a market-news recap cannot become a fresh industry call.

The review page includes `research_items` for today's and next morning's 09:00
windows: theme evidence, viewpoint changes, withdrawals, removed evidence, new
topics and capture gaps. Historical replay is archived without active delivery.
An edit's current observation does not backdate an alert to initial publication.
Acknowledgement marks research read and cannot approve a formal event. A later
successful surface capture marks its daily gap resolved while keeping the
failure record. This adds local delivery only, without sending external messages.
The digest uses stable ID cursors (`before_id`, `next_cursor`) and the UI can load
older current-window items beyond the first 100 without creating a history tab.

`collection_surface_runs` records each account/surface attempt, including empty
and failed results, requested/received counts and the observed publication
range. `bounded` and `range_complete` describe only the requested scope; neither
proves complete history. Restored legacy metadata is labelled `legacy_snapshot`;
where old counts were not retained, the UI says **条数未保留**, not an empty feed.
The current account summary remains compatible; surface coverage is separate.

Research startup does not start or repair FreeStockDB. Existing provider tasks
own its lifecycle. The market store initializes on demand; a locked/unavailable
warehouse produces 503 for market-dependent requests, while source/research
pages remain available. `ok` stays a compatibility process-liveness field;
`process_ready`, `business_ok`, `overall_status`, `data_status` and
`delivery_status` expose distinct operational facts. Unknown data never becomes
healthy solely because the API responds.

Useful read APIs: `/api/research-digest`, `/api/research-index/status`,
`/api/collection/surfaces`, `/api/posts/{post_id}/observations`, and
`/api/theme-candidates`. Topic writes use `/api/research-themes` and
`/api/theme-candidates/{id}/decision`; research acknowledgement uses
`/api/research-digest/{id}/acknowledge`.

The versioned `theme_catalog.json` contains explicit theme aliases and research
associations. Adding aliases expands research discovery; it does not change
event approval rules. Broad words such as “酒” must not collapse white spirits
and yellow rice wine into the same theme.

`GET /api/theme-leads` is read-only and paginated. Theme summaries retain the
earliest saved source across the local history even when the evidence list has
a date filter. `POST /api/theme-leads/extract` replays the local index without
requesting a market sync or model run.

Summaries distinguish the earliest saved source and earliest original source.
`first_original_*` retains the oldest authored post, including recaps and product
mentions. The separate `first_research_*` fields select original discussion
with analysis/forward/viewpoint evidence, excluding recaps, products, secondary
sources and withdrawals. The card labels this **最早原创讨论**; neither proves
a profitable opportunity or a complete research report.
Evidence items separately classify forward discussion, recommendations,
analysis and recaps. These are evidence categories, not automatic promotion
or investment rankings. A direct industry observation can remain `analysis`
even if it is useful for research.

Normal collection updates this index even when stock lead reconciliation is
deferred by a market lock. The review workbench continues to expose only today's
review and the next morning preview.

## Acceptance example

The yellow rice wine trace must distinguish 投研荟's saved September 2 source,
龙头18868's September statements, product purchases and later retrospective
claims, and 郑国成's collected material. An absent answer match is not evidence
that his articles and pins never mentioned the subject. Searching 会稽山 must also find
yellow rice wine sector evidence while identifying the security association as
research context.

The inventory also contains earlier aggregated text and company references.
Those cannot be used to move the verified original industry discussion
backward. 回顾 claims retain their own later publication time; product
references do not become formal stock recommendations.

The October 2 bounded collection of 郑国成 and 龙头18868 recovered 129 new
posts across the answer, article and pin surfaces. It found 龙头18868's
[September 9 pin](https://www.zhihu.com/pin/2080961343750923954), six days
earlier than the previously saved September 15 answer. His claim of an August
quantitative signal remains a later statement, not an August source record.
The 57 newly recovered 郑国成 articles/pins have no local yellow rice wine
keyword matches. 龙头18868's pin coverage is partial due to a returned row
missing identity/text/question fields; it must not be labelled complete.

## Focused validation

```powershell
python -m unittest _automation.trading_research.tests.test_theme_leads `
  _automation.trading_research.tests.test_collection_cli
python -m pytest _automation/trading_research/tests/test_zhihu_profile_coverage.py `
  _automation/hermes-capture/tests/test_zhihu_profile_capture.py -q
# Requires the console UI's existing jsdom dependency:
node --test _automation/hermes-capture/tests/test_zhihu_profile_script.cjs
```

The browser-script checks execute the actual fetch JavaScript with controlled
API responses, including HTTP-200 authentication errors, bounded 10003 retries,
pin content arrays, and partial pagination failure. UI tests and Playwright
flows verify source attribution, server pagination, and desktop/mobile access.

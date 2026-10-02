# KOL theme radar

Status date: 2026-10-02.

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
- `posted_at` is the source publication time. The earliest matching saved post
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

The versioned `theme_catalog.json` contains explicit theme aliases and research
associations. Adding aliases expands research discovery; it does not change
event approval rules. Broad words such as “酒” must not collapse white spirits
and yellow rice wine into the same theme.

`GET /api/theme-leads` is read-only and paginated. Theme summaries retain the
earliest saved source across the local history even when the evidence list has
a date filter. `POST /api/theme-leads/extract` replays the local index without
requesting a market sync or model run.

Summaries distinguish the earliest saved source and earliest original source.
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

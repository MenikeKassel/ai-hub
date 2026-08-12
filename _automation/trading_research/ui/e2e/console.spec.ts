import { expect, test, type Page } from '@playwright/test'

const event = {
  event_id: 'KOL-TEST-1', kol_name: '演示研究员', platform: 'X', source_url: 'https://x.com/fixture/status/1',
  source_note: '', posted_at: '2026-07-16T20:00:00+08:00', symbol: '002414', security_name: '高德红外',
  direction: 'long', thesis: '原帖明确看多并给出业绩逻辑', status: 'active', exclusion_reason: '',
  baseline_rule: 'next_open', baseline_date: '2026-07-17', baseline_price_raw: '13.31', benchmark_symbol: '000300',
  execution_warning: '', activated_at: '', updated_at: '', followups: [],
  latest_mark: { trade_date: '2026-07-17', directional_return: '0.02', benchmark_return: '0.005', directional_excess_return: '0.015', max_favorable_return: '0.03', max_adverse_return: '-0.01', tracking_days: '1' },
  technical_context: {
    event_id: 'KOL-TEST-1', feature_version: 'technical-context-v1', input_hash: 'fixture', symbol: '002414',
    posted_at: '2026-07-16T20:00:00+08:00', as_of_trade_date: '2026-07-16', adjustment: 'qfq',
    rsi14: 62.1, macd_dif: 0.2, macd_dea: 0.15, macd_hist: 0.05, macd_hist_pct: 0.004,
    atr14: 0.6, atr14_pct: 0.045, volume_ratio_5: 1.2, return_20d: 0.08, distance_60d_high: -0.03,
    history_bars: 80, status: 'complete', warnings: [], source_hash: 'fixture', computed_at: '2026-07-17T20:00:00+08:00',
  },
}

const events = [event, ...Array.from({ length: 25 }, (_, index) => ({
  ...event,
  event_id: `KOL-TEST-${index + 2}`,
  kol_name: `研究员${index + 1}`,
  source_url: `https://x.com/fixture/status/${index + 2}`,
  posted_at: `2026-07-${String(15 - (index % 10)).padStart(2, '0')}T10:00:00+08:00`,
  symbol: String(600000 + index),
  security_name: `测试股票${index + 1}`,
  thesis: `用于验证长列表滚动与搜索的推荐理由 ${index + 1}`,
  baseline_date: '2026-07-16',
  latest_mark: { ...event.latest_mark, trade_date: '2026-07-17' },
}))]

const market = {
  ok: true, active_instruments: 1, coverage_count: 2, warning_count: 0, recent_issue_count: 0,
  latest_open_date: '2026-07-17', latest_daily_date: '2026-07-17', daily_data_status: 'current',
  lagging_symbols: [], lagging_symbol_count: 0, coverage: [], issues: [], runs: [], queue: [],
}

const health = {
  ok: true, twitter_cli: 'twitter.exe', twitter_credentials_configured: true, twitter_auth_status: 'ok',
  nitter_credentials_configured: true, nitter_ready: true, redis_ready: true, fallback_mode: 'shadow',
  codex_cli: 'codex.exe', unlimited_ocr_available: true, rapid_ocr_available: true,
  ocr_provider: 'rapidocr', rapid_ocr_runtime: 'venv-ocr-fast', database: 'posts.db', media_bytes: 1024,
  morning_initial_task: 'installed', morning_refresh_task: 'installed', morning_pipeline_task: 'installed', market_sync_task: 'installed', return_task: 'installed', market,
  pipeline_refresh: { status: 'completed', phase: 'done', symbols: ['002414'], as_of: '2026-07-17', error: '' },
  morning_runs: [{ run_id: 'morning-1', review_date: '2026-07-17', status: 'completed', reviewed_posts: 3, ready_drafts: 2, attention_drafts: 1, failed_posts: 0 }],
}

const mentions = Array.from({ length: 50 }, (_, index) => ({
  id: index + 1, post_id: `2078000000000000${String(index).padStart(3, '0')}`, kol_id: 1,
  display_name: '演示研究员', handle: 'fixture', url: `https://x.com/fixture/status/${index + 1}`,
  text: `历史原帖 ${index + 1}`, article_text: '', ocr_text: '', posted_at: '2026-07-17T08:00:00+08:00',
  review_status: 'pending', symbol: String(600000 + index), security_name: `历史股票${index + 1}`,
  instrument_type: 'stock', direction: 'long', mention_kind: 'analysis', evidence_text: `证据摘要 ${index + 1}`,
  extraction_method: 'fixture', confidence: 0.9, status: 'pending', auto_confirmed: false, review_note: '', related_event_ids: [],
}))

async function mockApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = []
    if (path === '/api/events') body = events
    else if (path.match(/^\/api\/events\/[^/]+\/research$/)) body = {
      status: 'partial',
      snapshot_id: 'research-fixture',
      warnings: [],
      data: {
        version: 'event-method-research-v1', event_id: 'KOL-TEST-1', symbol: '002414',
        posted_at: '2026-07-16T20:00:00+08:00', as_of_trade_date: '2026-07-16',
        status: 'partial', computed_at: '2026-07-17T20:00:00+08:00',
        data_lineage: { minute_bars: 240, minute_session_mode: 'complete_session' },
        warnings: [],
        lenses: {
          short_term_leader: { status: 'ready', label: '短线龙头', conclusion: 'candidate', facts: { universe_size: 5200, return_20d: 0.2, distance_60d_high: -0.01, strongest_board_rps50: 92, candidate_types: { trend_leader: { candidate: true }, limit_up_leader: { candidate: false }, liquidity_core: { candidate: false }, board_leader: { candidate: true } } }, observations: ['trend_leader'], warnings: [] },
          dow_wave_gann: { status: 'partial', label: '道氏 / 波浪 / 江恩', conclusion: 'higher_high_higher_low', facts: { trend_structure: 'higher_high_higher_low', atr14_pct: 0.04, ma20: 12.5, ma60: 11.8, fib_context: { premium_discount: 'premium' } }, observations: [], warnings: ['wave_count_requires_manual_interpretation'] },
          price_action: { status: 'ready', label: 'PA 价格行为', conclusion: 'descriptive_only', facts: { regime: 'trend', trend_structure: 'higher_high_higher_low', directional_efficiency_20: 0.6, bar_overlap_20: 0.2, breakout_vs_prior_20: 'up', latest_close_location: 0.8 }, observations: [], warnings: [] },
          ict: { status: 'partial', label: 'ICT 时间与流动性', conclusion: 'descriptive_only', facts: { post_session: 'post_close', distance_to_prior_20_high: -0.01, distance_to_prior_20_low: 0.2, buy_side_liquidity_sweep: false, sell_side_liquidity_sweep: false, dealing_range: { premium_discount: 'premium' } }, observations: [], warnings: ['liquidity_levels_are_price_proxies_not_order_book_liquidity'] },
          wyckoff_orderflow: { status: 'partial', label: '威科夫 / 订单流', conclusion: 'manual_phase_review', facts: { wyckoff: { effort_result: 'high_effort_expansion', close_location_value: 0.8, spread_atr: 1.2 }, vwap: { session_vwap: 13.4 }, volume_profile: { poc: 13.3 }, tpo: { poc: 13.2 }, cvd: { status: 'unavailable' }, option_wall: { status: 'not_applicable' } }, observations: [], warnings: ['cvd_unavailable_without_aggressor_side_trades'] },
        },
      },
      interpretation: {
        interpretation_id: 'ai-fixture', provider: 'fixture-ai', model: 'fixture-ai',
        prompt_version: 'event-method-research-ai-v4', status: 'ready',
        validation: { ok: true }, created_at: '2026-07-17T20:01:00+08:00',
        payload: { interpretations: [
          { lens: 'short_term_leader', hypothesis: '趋势龙与板块龙候选证据同时存在。', confidence: 0.72, evidence_refs: ['lenses.short_term_leader.facts.candidate_types'], counter_evidence: [], invalidation: ['横截面排名跌出阈值'] },
          { lens: 'dow_wave_gann', hypothesis: '道氏结构偏上行，波浪计数仍需人工确认。', confidence: 0.58, evidence_refs: ['lenses.dow_wave_gann.facts.trend_structure'], counter_evidence: [], invalidation: [] },
          { lens: 'price_action', hypothesis: '价格行为处于趋势环境。', confidence: 0.7, evidence_refs: ['lenses.price_action.facts.regime'], counter_evidence: [], invalidation: [] },
          { lens: 'ict', hypothesis: '价格位于区间溢价区。', confidence: 0.52, evidence_refs: ['lenses.ict.facts.dealing_range'], counter_evidence: [], invalidation: [] },
          { lens: 'wyckoff_orderflow', hypothesis: '量价呈高努力扩张，但阶段仍需人工确认。', confidence: 0.55, evidence_refs: ['lenses.wyckoff_orderflow.facts.wyckoff'], counter_evidence: [], invalidation: [] },
        ] },
      },
    }
    else if (path.match(/^\/api\/events\/[^/]+\/revisions$/)) body = []
    else if (path === '/api/checkpoints') body = []
    else if (path === '/api/summary') body = { stock_leads: 10, confirmed_stock_leads: 4, pending_stock_leads: 0, pending_posts: 2, market }
    else if (path.startsWith('/api/market/daily/002414/indicators')) body = {
      symbol: '002414', adjustment: 'qfq', formula_version: 'daily-technical-v1',
      available_from: '2026-07-16', available_to: '2026-07-17', row_count: 2,
      rows: [
        { symbol: '002414', trade_date: '2026-07-16', open: 13, high: 13.5, low: 12.8, close: 13.2, volume: 1000, adjustment: 'qfq', provider: 'fixture', ma5: 13.1, macd_hist: 0.01, rsi14: 56 },
        { symbol: '002414', trade_date: '2026-07-17', open: 13.3, high: 13.8, low: 13.1, close: 13.6, volume: 1200, adjustment: 'qfq', provider: 'fixture', ma5: 13.2, macd_hist: 0.02, rsi14: 60 },
      ],
    }
    else if (path === '/api/kols') body = []
    else if (path === '/api/digest-authors') body = [{
      author_name: '示例知乎博主', normalized_name: '示例知乎博主', status: 'attributed_only', source_kind: 'secondhand_aggregation',
      summary_count: 3, latest_posted_at: '2026-07-19T18:00:00+08:00',
      latest_source_url: 'https://www.zhihu.com/question/1/answer/2', latest_title: '如何评价今日A股行情？',
      latest_summary: '本周主题：关注能源与红利。', symbols: ['600900'],
      profile_handle: 'fixture-author', profile_url: 'https://www.zhihu.com/people/fixture-author',
      tracking_status: 'linked_only', profile_updated_at: '2026-07-20T09:00:00+08:00',
    }]
    else if (path === '/api/kol-leaderboard') body = { policy: {}, rows: [] }
    else if (path === '/api/stock-leads') body = []
    else if (path === '/api/stock-mentions') body = { items: mentions, total: 1500, page: 1, page_size: 50, total_pages: 30 }
    else if (path === '/api/pipeline/status') body = { latest_fetch_at: '2026-07-18T19:08:00+08:00', latest_fetch_status: 'success', latest_ai_at: '2026-07-18T20:00:00+08:00', pending_ai: 0, morning_delivery: { status: 'ready', completed_at: '2026-07-18T08:55:00+08:00' }, next_preview: {}, latest_trade_date: '2026-07-17', expected_trade_date: '2026-07-17', market_status: 'closed', lagging_symbols: [], refresh: { status: 'completed', phase: 'done', symbols: [], error: '' } }
    else if (path === '/api/system/health') body = health
    else if (path === '/api/fetch-runs') body = []
    await route.fulfill({ json: body })
  })
}

test.beforeEach(async ({ page }) => { await mockApi(page) })

for (const [hash, heading] of [
  ['events', '正式事件'],
  ['backtests', '收益审计'],
  ['kols', 'KOL管理'],
  ['system', '数据健康'],
  ['history', '历史归档'],
] as const) {
  test(`${heading} page matches the consolidated workflow`, async ({ page }) => {
    await page.goto(`/#/${hash}`)
    await expect(page.getByRole('heading', { name: heading, exact: true }).first()).toBeVisible()
    await expect(page.locator('.sidebar .nav-item:visible, .mobile-nav button:visible')).toHaveCount(7)
    expect(await page.evaluate(() => document.body.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  })
}

test('return audit renders real-price candlesticks for a tracked event', async ({ page }) => {
  await page.goto('/#/backtests')
  await expect(page.getByText(/这里只展示你已批准的正式推荐事件/)).toBeVisible()
  await expect(page.getByText('股票线索', { exact: true })).toHaveCount(0)
  await expect(page.getByText('002414', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('2026-07-17 收盘 · qfq')).toBeVisible()
  await expect(page.locator('canvas').first()).toBeVisible()
  await page.getByText('推荐时市场状态').click()
  await expect(page.getByText('RSI14', { exact: true })).toBeVisible()
  await page.getByText('多方法研究', { exact: true }).click()
  await expect(page.getByText('短线龙头', { exact: true })).toBeVisible()
  await expect(page.getByText('威科夫 / 订单流', { exact: true })).toBeVisible()
  await expect(page.getByText('AI假设', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('真实 CVD', { exact: true })).toBeVisible()
  await expect(page.getByText('买入信号')).toHaveCount(0)
  await expect(page.getByText('最大有利 MFE')).toBeVisible()
})

test('KOL management separates attributed Zhihu sources from ranked KOLs', async ({ page }) => {
  await page.goto('/#/kols')
  await page.getByRole('button', { name: /知乎署名来源/ }).click()
  await expect(page.getByText('示例知乎博主')).toBeVisible()
  await expect(page.getByText(/只作二手线索，不直接计入推荐收益/)).toBeVisible()
  await expect(page.getByText('600900')).toBeVisible()
  await expect(page.getByTitle('打开原作者主页')).toHaveAttribute('href', 'https://www.zhihu.com/people/fixture-author')
})

test('return audit keeps a long stock list inside an independently scrolling workspace', async ({ page }) => {
  await page.goto('/#/backtests')
  const list = page.locator('.audit-event-scroll')
  await expect(list).toBeVisible()
  const dimensions = await list.evaluate((element) => ({ client: element.clientHeight, scroll: element.scrollHeight }))
  expect(dimensions.scroll).toBeGreaterThan(dimensions.client)
  await page.getByLabel('搜索跟踪股票').fill('测试股票12')
  await expect(page.getByRole('button', { name: /600011.*测试股票12/ })).toBeVisible()
  await expect(page.getByText('1 / 26')).toBeVisible()
})

test('formal events supports dense status filtering and search', async ({ page }) => {
  await page.goto('/#/events')
  await page.getByLabel('搜索正式事件').fill('研究员8')
  await expect(page.getByText('测试股票8', { exact: true })).toBeVisible()
  await expect(page.getByText('显示 1 条')).toBeVisible()
  const manager = page.locator('.event-manager')
  const box = await manager.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.height).toBeLessThanOrEqual(700)
})

test('history archive paginates a large server-side result without page overflow', async ({ page }) => {
  await page.goto('/#/history')
  await expect(page.getByText('1500 条')).toBeVisible()
  await expect(page.getByText('第 1 / 30 页 · 每页 50 条')).toBeVisible()
  await expect(page.locator('.mention-index tbody tr')).toHaveCount(50)
  expect(await page.evaluate(() => document.body.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
})

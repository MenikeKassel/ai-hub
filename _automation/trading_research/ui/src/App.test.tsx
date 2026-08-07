import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const reviewDate = new Date().toLocaleDateString('en-CA')
const draft = {
  id: 1, post_id: '2078000000000000123', symbol: '605178', security_name: '时空科技',
  direction: 'long', action: 'watch', horizon: 'short', strength: 'explicit', thesis: '列入当日个股分享，原帖未提供具体个股理由',
  evidence_type: 'original_pre_event', evidence_spans: ['1.时空科技'], evidence_source: 'text',
  conditions: [], depends_on_ocr: false, mention_kind: 'recommendation', confidence: 0.99,
  status: 'ready', attention_reasons: [], queue_scope: 'morning', review_date: reviewDate,
  review_note: '', event_id: '', reviewed_at: '', extraction_version: 'v1', model_name: 'codex-batch',
  url: 'https://x.com/example/status/2078000000000000123',
  text: '马前炮：7月17日个股分享\n1.时空科技\n内部学习群388一个月',
  article_title: '', article_text: '', quoted_text: '', posted_at: `${reviewDate}T08:30:00+08:00`,
  post_type: 'original', provider_warning: '', review_status: 'pending', display_name: '示例KOL',
  handle: 'example', local_media: [],
}
const post = {
  ...draft,
  kol_id: 1, author_name: '示例KOL', domain: 'A股', media: [], metrics: {},
  reply_to_id: '', reply_to_author: '', quoted_author: '', review_note: '', rule_score: 10,
  rule_reasons: ['fixture'], rule_symbols: ['605178'], rule_direction: 'long', content_type: 'recommendation',
  is_candidate: true, model_status: 'completed', model_summary: '明确列出一只股票', model_error: '',
  ocr_status: 'not_needed', ocr_attempts: 0, ocr_text: '', ocr_error: '', ocr_updated_at: '',
  source_note: '', notion_url: '', canonical_provider: 'fixture', metrics_provider: 'fixture',
}
const failedPost = {
  ...post,
  id: 2, post_id: '2078000000000000456', url: 'https://x.com/example/status/2078000000000000456',
  text: '图片中包含今日股票清单', model_status: 'failed', model_summary: '', model_error: 'OCR timeout',
  is_candidate: true, rule_symbols: [], local_media: [],
}
const zhihuAnalysisPost = {
  ...post,
  post_id: '2063325055573143758',
  platform: 'Zhihu',
  handle: 'zui-hou-de-qing-yu-83-86',
  display_name: '世间已再无留恋',
  url: 'https://www.zhihu.com/question/2060450831401497712/answer/2063325055573143758',
  text: '分析股指期货当前做空赔率，不建议新开空单。',
  content_type: 'market_view',
  evidence_type: 'original_pre_event',
  model_summary: '文中个股仅作为历史类比，不构成当前个股推荐。',
  drafts: [],
}
const approvedDraft = {
  ...draft,
  id: 3, post_id: '2078000000000000789', symbol: '000938', security_name: '紫光股份',
  text: '今日个股分享：紫光股份', evidence_spans: ['紫光股份'], status: 'approved',
  event_id: 'KOL-0031', reviewed_at: `${reviewDate}T09:00:00+08:00`,
}
const attentionDraft = {
  ...draft,
  id: 4, post_id: failedPost.post_id, symbol: '603893', security_name: '瑞芯微',
  status: 'needs_attention', attention_reasons: ['evidence_not_found'],
  thesis: '图片建议低吸瑞芯微', evidence_spans: ['建议关注瑞芯微'],
  evidence_source: 'ocr', depends_on_ocr: true,
  text: failedPost.text, ocr_text: '大盘环境较差，锚定低吸瑞芯微',
}
const morning = {
  review_date: reviewDate,
  summary: { new_posts: 2, ai_processed: 1, waiting_review: 1, ai_failed: 1, approved_today: 1 },
  delivery: { status: 'ready', deadline: `${reviewDate}T09:00:00+08:00`, completed_at: `${reviewDate}T08:55:00+08:00`, coverage: 1, active_kols: 12, successful_kols: 12, failed_kols: 0, errors: [] },
  posts: [post, failedPost],
  drafts: [draft],
  approved_drafts: [approvedDraft],
}

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
}

describe('KOL morning audit workbench', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    window.location.hash = '#/reviews'
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path.startsWith('/api/morning-review')) return response(morning)
      if (path === '/api/pipeline/status') return response({ latest_fetch_at: '', latest_fetch_status: 'success', latest_ai_at: '', pending_ai: 0, morning_delivery: morning.delivery, next_preview: morning.summary, latest_trade_date: reviewDate, expected_trade_date: reviewDate, market_status: 'current', lagging_symbols: [], refresh: { status: 'idle', symbols: [], error: '' } })
      if (path === '/api/events' || path === '/api/checkpoints' || path === '/api/kols') return response([])
      if (path === '/api/kol-leaderboard') return response({ policy: {}, rows: [] })
      if (path.startsWith('/api/stock-leads')) return response([])
      return response({})
    }))
  })

  it('opens on the morning queue with delivery metrics', async () => {
    renderApp()
    expect(await screen.findByRole('heading', { name: '今日审核' })).toBeInTheDocument()
    expect(await screen.findByText('准时完成')).toBeInTheDocument()
    expect(screen.getByText(/12 \/ 12 个账号完成采集/)).toBeInTheDocument()
    expect(screen.getByText('新增帖子')).toBeInTheDocument()
    expect(await screen.findByText('2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /待你确认\s*1/ })).toBeInTheDocument()
    expect(screen.getAllByText('1').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/内部学习群388一个月/).length).toBeGreaterThan(0)
    expect(screen.getByDisplayValue('列入当日个股分享，原帖未提供具体个股理由')).toBeInTheDocument()
    expect(screen.getByDisplayValue('关注')).toBeInTheDocument()
    expect(screen.getByDisplayValue('短线')).toBeInTheDocument()
  })

  it('uses every morning metric as a real queue filter', async () => {
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: /新增帖子\s*2/ }))
    expect(await screen.findByText('图片中包含今日股票清单')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /AI 失败\s*1/ }))
    expect(await screen.findByText(/OCR timeout/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /今日批准\s*1/ }))
    expect(await screen.findByLabelText('股票名称')).toHaveValue('紫光股份')
    expect(screen.getByLabelText('股票名称')).toBeDisabled()
  })

  it('shows analyzed Zhihu answers even when they do not create recommendation drafts', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path.startsWith('/api/morning-review')) return response({
        ...morning,
        summary: { ...morning.summary, new_posts: 1, ai_processed: 1, waiting_review: 0 },
        posts: [zhihuAnalysisPost],
        drafts: [],
        approved_drafts: [],
      })
      if (path === '/api/pipeline/status') return response({ lagging_symbols: [], refresh: {} })
      return response([])
    }))
    renderApp()

    fireEvent.click(await screen.findByRole('button', { name: /AI 已处理\s*1/ }))
    expect(await screen.findAllByText('知乎')).not.toHaveLength(0)
    expect(screen.getByText('文中个股仅作为历史类比，不构成当前个股推荐。')).toBeInTheDocument()
    expect(screen.getByText(/market_view \/ original_pre_event/)).toBeInTheDocument()
  })

  it('blocks invalid evidence and sends the corrected exact quote before approval', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.startsWith('/api/morning-review')) return response({
        ...morning,
        summary: { ...morning.summary, waiting_review: 1 },
        drafts: [attentionDraft],
      })
      if (path === '/api/recommendation-drafts/4' && init?.method === 'PATCH') return response({ ...attentionDraft, status: 'needs_attention', attention_reasons: ['image_dependency'] })
      if (path === '/api/recommendation-drafts/4/approve' && init?.method === 'POST') return response({ ...attentionDraft, status: 'approved', event_id: 'KOL-0032' })
      return response([])
    })
    vi.stubGlobal('fetch', fetchMock)
    renderApp()

    expect(await screen.findByRole('button', { name: /先修复证据/ })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/原文证据/), { target: { value: '大盘环境较差，锚定低吸瑞芯微' } })
    fireEvent.change(screen.getByLabelText('AI 错误类型'), { target: { value: 'wrong_evidence' } })
    const approve = screen.getByRole('button', { name: /保存并批准跟踪/ })
    expect(approve).toBeEnabled()
    fireEvent.click(approve)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/recommendation-drafts/4',
      expect.objectContaining({ method: 'PATCH' }),
    ))
    const patchCall = fetchMock.mock.calls.find(([input, init]) => String(input) === '/api/recommendation-drafts/4' && init?.method === 'PATCH')
    expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({
      evidence_spans: ['大盘环境较差，锚定低吸瑞芯微'],
      correction_type: 'wrong_evidence',
    })
  })

  it('saves one edited stock draft before approving it', async () => {
    let saved = { ...draft }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.startsWith('/api/morning-review')) return response({ ...morning, drafts: saved.status === 'ready' ? [saved] : [], approved_drafts: saved.status === 'approved' ? [saved] : morning.approved_drafts })
      if (path === '/api/recommendation-drafts/1' && init?.method === 'PATCH') {
        saved = { ...saved, ...JSON.parse(String(init.body)) }
        return response(saved)
      }
      if (path === '/api/recommendation-drafts/1/approve' && init?.method === 'POST') {
        saved = { ...saved, status: 'approved', event_id: 'KOL-0030' }
        return response(saved)
      }
      return response([])
    })
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    const thesis = await screen.findByLabelText('推荐理由')
    fireEvent.change(thesis, { target: { value: '人工核对后的原帖理由' } })
    fireEvent.change(screen.getByLabelText('AI 错误类型'), { target: { value: 'wrong_thesis' } })
    fireEvent.click(screen.getByRole('button', { name: /保存并批准跟踪/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/recommendation-drafts/1/approve',
      expect.objectContaining({ method: 'POST' }),
    ))
    const patchCall = fetchMock.mock.calls.find(([input, init]) => String(input) === '/api/recommendation-drafts/1' && init?.method === 'PATCH')
    expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({ symbol: '605178', thesis: '人工核对后的原帖理由', correction_type: 'wrong_thesis' })
  })

  it('creates a missed-stock draft from the original post detail', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.startsWith('/api/morning-review')) return response({ ...morning, drafts: [], approved_drafts: [] })
      if (path === `/api/posts/${post.post_id}/recommendation-drafts` && init?.method === 'POST') return response({ ...draft, id: 99 })
      return response({ lagging_symbols: [], refresh: {} })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: /新增帖子\s*2/ }))
    fireEvent.click(await screen.findByRole('button', { name: '补建草稿' }))
    fireEvent.change(screen.getByLabelText('补建股票代码'), { target: { value: '605178' } })
    fireEvent.change(screen.getByLabelText('补建股票名称'), { target: { value: '时空科技' } })
    fireEvent.change(screen.getByLabelText('补建推荐理由'), { target: { value: '原帖列入当日个股分享' } })
    fireEvent.change(screen.getByLabelText('补建原文证据'), { target: { value: '1.时空科技' } })
    fireEvent.change(screen.getByLabelText('补建纠错说明'), { target: { value: 'AI 漏掉第一只股票' } })
    fireEvent.click(screen.getByRole('button', { name: /保存草稿/ }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/posts/${post.post_id}/recommendation-drafts`, expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/recommendation-drafts') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ symbol: '605178', correction_type: 'missed_stock', note: 'AI 漏掉第一只股票' })
  })

  it('amends a formal event with an explicit recalculation warning', async () => {
    const event = {
      event_id: 'KOL-0099', kol_name: '示例KOL', platform: 'X', source_url: draft.url,
      source_note: `post:${draft.post_id}`, posted_at: draft.posted_at, symbol: '605178', security_name: '时空科技',
      direction: 'long', thesis: '原推荐理由', status: 'active', exclusion_reason: '', baseline_rule: 'same_close',
      baseline_date: reviewDate, baseline_price_raw: '20.10', benchmark_symbol: '000300', execution_warning: 'ok',
      activated_at: reviewDate, updated_at: reviewDate, latest_mark: { trade_date: reviewDate, directional_return: '0.01' },
    }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/events') return response([event])
      if (path === `/api/events/${event.event_id}/revisions`) return response([])
      if (path === `/api/events/${event.event_id}/amendments` && init?.method === 'POST') return response({ event: { ...event, symbol: '600900', security_name: '长江电力' }, revision: { recalculation_required: true }, refresh_status: 'queued' })
      return response({ lagging_symbols: [], refresh: {} })
    })
    vi.stubGlobal('fetch', fetchMock)
    window.location.hash = '#/events'
    renderApp()
    expect(await screen.findByText('时空科技')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('纠错正式事件'))
    fireEvent.change(screen.getAllByLabelText('股票代码')[0], { target: { value: '600900' } })
    fireEvent.change(screen.getAllByLabelText('股票名称')[0], { target: { value: '长江电力' } })
    fireEvent.change(screen.getByLabelText('正式事件修改原因'), { target: { value: '修正股票映射' } })
    expect(screen.getByText(/旧收益会备份并重新计算/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /确认修订/ }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/events/${event.event_id}/amendments`, expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('uses the six workflow navigation destinations', async () => {
    renderApp()
    for (const label of ['今日审核', '正式事件', '收益审计', 'KOL管理', '数据健康', '历史归档']) {
      expect(screen.getAllByRole('button', { name: new RegExp(label) }).length).toBeGreaterThan(0)
    }
    fireEvent.click(screen.getAllByRole('button', { name: /正式事件/ })[0])
    expect(await screen.findByRole('heading', { name: '正式事件' })).toBeInTheDocument()
  })

  it('shows event-time technical context and MFE without a trading score', async () => {
    const event = {
      event_id: 'KOL-CONTEXT-1', kol_name: '示例KOL', platform: 'X', source_url: draft.url,
      source_note: `post:${draft.post_id}`, posted_at: `${reviewDate}T16:00:00+08:00`, symbol: '600900', security_name: '长江电力',
      direction: 'long', thesis: '原帖中的推荐理由', status: 'active', exclusion_reason: '', baseline_rule: 'same_day_close',
      baseline_date: reviewDate, baseline_price_raw: '30.10', benchmark_symbol: '000300', execution_warning: '',
      activated_at: reviewDate, updated_at: reviewDate, followups: [],
      latest_mark: { trade_date: reviewDate, directional_return: '0.05', directional_excess_return: '0.03', max_favorable_return: '0.08', max_adverse_return: '-0.02', tracking_days: '5' },
      technical_context: {
        event_id: 'KOL-CONTEXT-1', feature_version: 'technical-context-v1', input_hash: 'fixture', symbol: '600900',
        posted_at: `${reviewDate}T16:00:00+08:00`, as_of_trade_date: reviewDate, adjustment: 'qfq', rsi14: 61.2,
        macd_dif: 0.1, macd_dea: 0.08, macd_hist: 0.02, macd_hist_pct: 0.001, atr14: 0.8, atr14_pct: 0.02,
        volume_ratio_5: 1.24, return_20d: 0.06, distance_60d_high: -0.03, history_bars: 80,
        status: 'complete', warnings: [], source_hash: 'fixture', computed_at: `${reviewDate}T20:00:00+08:00`,
      },
    }
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/events') return response([event])
      if (path === '/api/checkpoints') return response([])
      if (path === '/api/summary') return response({ market: { daily_data_status: 'current' } })
      if (path.startsWith('/api/market/daily/600900/indicators')) return response({
        symbol: '600900', adjustment: 'qfq', formula_version: 'daily-technical-v1',
        available_from: reviewDate, available_to: reviewDate, row_count: 1,
        rows: [
          { symbol: '600900', trade_date: reviewDate, open: 30, high: 31, low: 29.8, close: 30.5, volume: 1000, adjustment: 'qfq', provider: 'fixture', ma5: 30.2, macd_hist: 0.02, rsi14: 61.2 },
        ],
      })
      if (path === '/api/pipeline/status') return response({ lagging_symbols: [], refresh: {} })
      return response([])
    }))
    window.location.hash = '#/backtests'
    renderApp()

    expect(await screen.findByRole('heading', { name: '收益审计' })).toBeInTheDocument()
    fireEvent.click(await screen.findByText('推荐时市场状态'))
    expect(screen.getByText('RSI14')).toBeInTheDocument()
    expect(screen.getByText('61.2')).toBeInTheDocument()
    expect(screen.getByText('最大有利 MFE')).toBeInTheDocument()
    expect(screen.getByText('8.00%')).toBeInTheDocument()
    expect(screen.queryByText('技术评分')).not.toBeInTheDocument()
  })

  it('keeps the stock mention archive read-only', async () => {
    const lead = {
      id: 24, post_id: draft.post_id, kol_id: 1, display_name: '示例KOL', handle: 'example',
      url: draft.url, text: draft.text, posted_at: draft.posted_at, review_status: 'pending',
      symbol: '605178', security_name: '时空科技', instrument_type: 'stock', direction: 'long',
      mention_kind: 'recommendation', evidence_text: '1.时空科技', extraction_method: 'name_match',
      confidence: 0.9, status: 'pending', auto_confirmed: false, review_note: '',
    }
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).startsWith('/api/stock-mentions') ? response({ items: [lead], total: 1, page: 1, page_size: 50, total_pages: 1 }) : String(input) === '/api/pipeline/status' ? response({ lagging_symbols: [], refresh: {} }) : response([])))
    window.location.hash = '#/history'
    renderApp()
    expect(await screen.findByRole('heading', { name: '历史归档' })).toBeInTheDocument()
    expect(screen.queryByText('股票提及索引', { exact: false })).not.toBeInTheDocument()
    expect(await screen.findByText('605178')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /确认并加入数据池/ })).not.toBeInTheDocument()
  })
})

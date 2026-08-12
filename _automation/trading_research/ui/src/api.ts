import type {
  Checkpoint, DigestAuthor, Draft, DraftCorrectionType, DraftRevision, Event, EventAmendment, EventAmendmentResult, EventDossier, EventIntradayContext, EventMark, EventMethodResearchSection, EventRevision, EventTechnicalContext, EventUpdate, FetchRun, FoundationRefreshState, FreeStockDBHealth, Health, Instrument, Kol, KolLeaderboard, KolPerformanceDetail, KolPerformanceResponse, KolPerformanceRow, ManualRecommendationDraft, MarketDailyBar, MarketHealth, MarketIndicatorSeries, MorningReview, OperatorTasks, PipelineStatus, Post, RecommendationDraft, ReviewAgentDecision, ReviewAgentSummary, ReviewResult, StockLead, StockMentionPage, Summary,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item: { msg?: string }) => item.msg || response.statusText).join('；')
      : body.detail
    throw new Error(detail || response.statusText)
  }
  return response.json() as Promise<T>
}

export const api = {
  summary: () => request<Summary>('/api/summary'),
  kols: () => request<Kol[]>('/api/kols'),
  digestAuthors: () => request<DigestAuthor[]>('/api/digest-authors'),
  kolLeaderboard: () => request<KolLeaderboard>('/api/kol-leaderboard'),
  kolPerformance: (params: { platform?: string; window?: string; horizon?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.platform && params.platform !== 'all') query.set('platform', params.platform)
    if (params.window) query.set('window', params.window)
    if (params.horizon) query.set('horizon', params.horizon)
    return request<KolPerformanceResponse>(`/api/kol-performance${query.toString() ? `?${query.toString()}` : ''}`)
  },
  kolPerformanceDetail: (key: string) => request<KolPerformanceDetail>(`/api/kol-performance/${encodeURIComponent(key)}`),
  kolPerformanceSeries: (key: string, horizon = '1W', range = 'all') => request<{ points: Array<{ as_of: string; payload: KolPerformanceRow }>; point_count: number }>(`/api/kol-performance/${encodeURIComponent(key)}/series?horizon=${horizon}&range=${range}`),
  refreshKolPerformance: (asOf?: string) => request<{ ok: boolean; as_of: string; run_id: string; snapshots_inserted: number; coverage: KolPerformanceResponse['coverage'] }>('/api/kol-performance/refresh', { method: 'POST', body: JSON.stringify(asOf ? { as_of: asOf } : {}) }),
  createKol: (value: Pick<Kol, 'display_name' | 'handle' | 'domain' | 'platform'>) =>
    request<Kol>('/api/kols', { method: 'POST', body: JSON.stringify(value) }),
  patchKol: (id: number, value: Partial<Kol>) =>
    request<Kol>(`/api/kols/${id}`, { method: 'PATCH', body: JSON.stringify(value) }),
  queueKolBackfill: (id: number, count = 200) =>
    request<Kol>(`/api/kols/${id}/backfill`, { method: 'POST', body: JSON.stringify({ count }) }),
  posts: (query = '') => request<Post[]>(`/api/posts${query ? `?${query}` : ''}`),
  post: (id: string) => request<Post>(`/api/posts/${id}`),
  classify: (id: string) => request<Post>(`/api/posts/${id}/classify`, { method: 'POST' }),
  review: (id: string, action: string, note: string, drafts: Draft[] = [], unified = false) =>
    request<ReviewResult>(`/api/posts/${id}/review`, {
      method: 'POST',
      body: JSON.stringify({
        action,
        note,
        drafts,
        confirm_leads: unified && action === 'approve',
        refresh_returns: unified && action === 'approve',
      }),
    }),
  morningReview: (reviewDate: string, historyPage = 1) => request<MorningReview>(
    `/api/morning-review?review_date=${encodeURIComponent(reviewDate)}&include_history=true&history_page=${historyPage}`,
  ),
  reprocessRecommendationPost: (postId: string) => request<{ ok: boolean; drafts: RecommendationDraft[]; draft_generation_status: string; draft_generation_error: string }>(
    `/api/posts/${postId}/recommendation-reprocess`, { method: 'POST' },
  ),
  recommendationDraft: (id: number) => request<RecommendationDraft>(`/api/recommendation-drafts/${id}`),
  patchRecommendationDraft: (id: number, value: Partial<Draft> & { note?: string; correction_type?: DraftCorrectionType }) =>
    request<RecommendationDraft>(`/api/recommendation-drafts/${id}`, {
      method: 'PATCH', body: JSON.stringify(value),
    }),
  approveRecommendationDraft: (id: number, note = '') =>
    request<RecommendationDraft>(`/api/recommendation-drafts/${id}/approve`, {
      method: 'POST', body: JSON.stringify({ note }),
    }),
  rejectRecommendationDraft: (id: number, note = '') =>
    request<RecommendationDraft>(`/api/recommendation-drafts/${id}/reject`, {
      method: 'POST', body: JSON.stringify({ note }),
    }),
  retryRecommendationDraft: (id: number) =>
    request<{ ok: boolean; drafts: RecommendationDraft[] }>(`/api/recommendation-drafts/${id}/retry`, { method: 'POST' }),
  recommendationDraftRevisions: (id: number) => request<DraftRevision[]>(`/api/recommendation-drafts/${id}/revisions`),
  createManualRecommendationDraft: (postId: string, value: ManualRecommendationDraft) =>
    request<RecommendationDraft>(`/api/posts/${postId}/recommendation-drafts`, {
      method: 'POST', body: JSON.stringify(value),
    }),
  fetchPosts: (maxCount = 50) =>
    request('/api/fetch', { method: 'POST', body: JSON.stringify({ max_count: maxCount, classify: false }) }),
  stockLeads: (query = '') => request<StockLead[]>(`/api/stock-leads${query ? `?${query}` : ''}`),
  stockMentions: (params: URLSearchParams) => request<StockMentionPage>(`/api/stock-mentions?${params}`),
  extractStockLeads: () => request('/api/stock-leads/extract', { method: 'POST' }),
  reviewStockLead: (id: number, value: { action: string; note: string; symbol?: string; security_name?: string }) =>
    request<StockLead>(`/api/stock-leads/${id}/review`, { method: 'POST', body: JSON.stringify(value) }),
  instruments: () => request<Instrument[]>('/api/instruments'),
  patchInstrument: (symbol: string, value: Partial<Instrument>) =>
    request<Instrument>(`/api/instruments/${symbol}`, { method: 'PATCH', body: JSON.stringify(value) }),
  enqueueMarketSync: (symbols: string[] = []) =>
    request<{ ok: boolean; queued_symbols: string[] }>('/api/market/sync', {
      method: 'POST', body: JSON.stringify({ symbols }),
    }),
  marketHealth: () => request<MarketHealth>('/api/market/health'),
  foundationRefresh: (asOf = 'auto', notify = true) => request<{ ok: boolean; status: string; as_of?: string }>('/api/market/foundation-refresh', {
    method: 'POST', body: JSON.stringify({ as_of: asOf, notify }),
  }),
  foundationRefreshStatus: () => request<FoundationRefreshState>('/api/market/foundation-refresh'),
  freestockdbHealth: () => request<FreeStockDBHealth>('/api/market/providers/freestockdb'),
  updateFreeStockDB: (dryRun = false) => request<{ ok: boolean; status: string; dry_run: boolean }>('/api/market/providers/freestockdb/update', {
    method: 'POST', body: JSON.stringify({ dry_run: dryRun }),
  }),
  marketDaily: (symbol: string, start?: string, end?: string) => {
    const params = new URLSearchParams({ adjustment: 'qfq' })
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    return request<MarketDailyBar[]>(`/api/market/instruments/${symbol}/daily?${params}`)
  },
  marketIndicators: (symbol: string, start?: string, end?: string) => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    const query = params.toString()
    return request<MarketIndicatorSeries>(`/api/market/daily/${symbol}/indicators${query ? `?${query}` : ''}`)
  },
  events: () => request<Event[]>('/api/events'),
  patchEvent: (id: string, value: EventUpdate) =>
    request<Event>(`/api/events/${id}`, { method: 'PATCH', body: JSON.stringify(value) }),
  amendEvent: (id: string, value: EventAmendment) =>
    request<EventAmendmentResult>(`/api/events/${id}/amendments`, { method: 'POST', body: JSON.stringify(value) }),
  eventRevisions: (id: string) => request<EventRevision[]>(`/api/events/${id}/revisions`),
  eventSeries: (id: string) => request<EventMark[]>(`/api/events/${id}/series`),
  eventFeatures: (id: string) => request<EventTechnicalContext>(`/api/events/${id}/features`),
  eventIntradayContext: (id: string) => request<EventIntradayContext>(`/api/events/${id}/intraday-context`),
  backfillEventIntraday: (id: string) => request<{ ok: boolean; status: string; event_id: string }>(`/api/events/${id}/intraday-backfill`, { method: 'POST' }),
  eventDossier: (id: string) => request<EventDossier>(`/api/events/${id}/dossier`),
  eventResearch: (id: string) => request<EventMethodResearchSection>(`/api/events/${id}/research`),
  eventResearchHistory: (id: string) => request<Record<string, unknown>[]>(`/api/events/${id}/research/history`),
  refreshEventResearch: (id: string, withAi = false) => request<{ ok: boolean; event_id: string; snapshot_id: string; status: string }>(`/api/events/${id}/research/refresh?with_ai=${withAi}`, { method: 'POST' }),
  eventMarketSeries: (id: string, adjustment = 'qfq') => request<MarketDailyBar[]>(`/api/events/${id}/market-series?adjustment=${adjustment}`),
  refreshEventDossier: (id: string) => request<{ ok: boolean; status: string; event_id: string; section_status: Record<string, string> }>(`/api/events/${id}/data-refresh`, { method: 'POST' }),
  exportEventDossier: (id: string) => `/api/events/${id}/dossier/export`,
  checkpoints: () => request<Checkpoint[]>('/api/checkpoints'),
  health: () => request<Health>('/api/system/health'),
  reviewAgentSummary: () => request<ReviewAgentSummary>('/api/review-agent/summary'),
  reviewAgentDecisions: (query = '') => request<ReviewAgentDecision[]>(`/api/review-agent/decisions${query ? `?${query}` : ''}`),
  patchReviewAgentSettings: (mode: 'shadow' | 'enabled') =>
    request<ReviewAgentSummary['settings']>('/api/review-agent/settings', {
      method: 'PATCH', body: JSON.stringify({ mode }),
    }),
  rollbackReviewAgentDecision: (id: number) =>
    request<ReviewAgentDecision>(`/api/review-agent/decisions/${id}/rollback`, { method: 'POST' }),
  pipelineStatus: () => request<PipelineStatus>('/api/pipeline/status'),
  fetchRuns: () => request<FetchRun[]>('/api/fetch-runs'),
  tasks: () => request<OperatorTasks>('/api/tasks'),
  startTask: (task: 'morning' | 'fetch' | 'zhihu') =>
    request<{ ok: boolean; task: string; started_at: string }>(`/api/tasks/${task}/start`, { method: 'POST' }),
  saveCredentials: (authToken: string, ct0: string) =>
    request('/api/system/twitter-credentials', {
      method: 'POST', body: JSON.stringify({ auth_token: authToken, ct0 }),
    }),
  saveNitterCredentials: (authToken: string, ct0: string) =>
    request('/api/system/nitter-credentials', {
      method: 'POST', body: JSON.stringify({ auth_token: authToken, ct0 }),
    }),
  saveDeepSeekCredentials: (apiKey: string) =>
    request('/api/system/opencode-go-credentials', {
      method: 'POST', body: JSON.stringify({ api_key: apiKey }),
    }),
}

export function percent(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}%` : '-'
}

export function formatDate(value?: string): string {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, History, Radar, RefreshCw, Search, ShieldAlert } from 'lucide-react'
import { api, formatDate } from '../api'
import QueryState from '../components/QueryState'
import { TopicDiscovery, SourceVersions } from '../features/research/ResearchPanels'
import type { ThemeLeadItem, ThemeLeadKind, ThemeLeadMappedSymbol, ThemeLeadSourceCoverage, ThemeLeadSummary, ThemeLeadSourceRole } from '../types'
import { updateRoute, useRoute } from '../workspace'

const kindOptions: Array<{ value: ThemeLeadKind; label: string }> = [
  { value: 'prospective', label: '前瞻' },
  { value: 'recommendation', label: '推荐' },
  { value: 'analysis', label: '分析' },
  { value: 'retrospective', label: '回顾' },
  { value: 'product', label: '产品' },
  { value: 'secondhand', label: '二手' },
]

const kindLabels: Record<string, string> = Object.fromEntries(kindOptions.map(({ value, label }) => [value, label]))
const sourceRoleLabels: Record<ThemeLeadSourceRole, string> = {
  original: '原帖', quoted: '引用', secondhand: '二手来源', aggregation: '聚合',
}
const fieldLabels: Record<string, string> = {
  text: '原帖正文', article_title: '文章标题', article_text: '文章正文', quoted_text: '引用正文', ocr_text: '图片文字',
}
const sourceSurfaceOrder = ['answers', 'articles', 'ideas'] as const
const sourceSurfaceLabels: Record<string, string> = { answers: '回答', articles: '文章', ideas: '想法' }
const coverageStatusLabels: Record<string, string> = {
  complete: '完成当前范围', empty: '完成当前范围', bounded: '有限分页', partial: '部分缺失',
  failed: '失败', unsupported: '失败', legacy_answers_only: '有限分页',
}
const providerWarningLabels: Record<string, string> = {
  row_missing_id_text_or_question: '想法中部分记录缺少标识或正文，已保留有效记录',
}
const providerSurfaceLabels: Record<string, string> = { answers: '回答', articles: '文章', ideas: '想法' }

function safeDate(value?: string | null) {
  return value ? formatDate(value) : '—'
}

function itemText(item: ThemeLeadItem) {
  return item.text || item.article_text || item.quoted_text || item.ocr_text || item.evidence_text || '原文内容未随索引返回。'
}

function symbolLabel(symbol: ThemeLeadMappedSymbol) {
  return `${symbol.symbol} ${symbol.name}`.trim()
}

function evidenceFieldLabel(field: string, contextRole?: string) {
  if (field === 'article_title' && contextRole === 'question') return '问题标题（不是回答作者观点）'
  if (contextRole === 'withdrawal') return '已不再推荐'
  return fieldLabels[field] || field
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function coverageRoot(coverage: ThemeLeadSourceCoverage) {
  if (isRecord(coverage.surfaces)) return coverage
  if (isRecord(coverage.profile_coverage)) return coverage.profile_coverage as ThemeLeadSourceCoverage
  return coverage
}

function coverageStatusLabel(status?: string | null) {
  if (!status) return '未说明'
  return coverageStatusLabels[status.toLowerCase()] || `状态：${status}`
}

function sourceSurfaceKey(item: ThemeLeadItem) {
  const value = String(item.source_surface || '').toLowerCase()
  if (value === 'answer') return 'answers'
  if (value === 'article') return 'articles'
  if (value === 'idea' || value === 'pin' || value === 'pins') return 'ideas'
  if (sourceSurfaceOrder.includes(value as typeof sourceSurfaceOrder[number])) return value
  const postType = String(item.post_type || '').toLowerCase()
  if (postType === 'answer' || postType === 'aggregation') return 'answers'
  if (postType === 'article') return 'articles'
  if (postType === 'idea' || postType === 'pin') return 'ideas'
  return null
}

function sourceSurfaceLabel(value: string | null) {
  return value ? sourceSurfaceLabels[value] || value : ''
}

function coverageStatuses(coverage: ThemeLeadSourceCoverage | null | undefined) {
  if (!coverage) return []
  const root = coverageRoot(coverage)
  const surfaces = isRecord(root.surfaces) ? root.surfaces as Record<string, ThemeLeadSourceCoverage> : {}
  const requested = new Set((root.requested_surfaces || []).map((surface) => String(surface).toLowerCase()))
  const keys = new Set<string>([
    ...sourceSurfaceOrder,
    ...requested,
    ...Object.keys(surfaces),
  ])
  if (!keys.size) return []
  return Array.from(keys).map((surface) => {
    const detail = isRecord(surfaces[surface]) ? surfaces[surface] : null
    const status = detail?.status || (requested.has(surface) ? root.status : null)
    const isRequested = requested.has(surface) || Boolean(detail)
    return {
      surface,
      label: sourceSurfaceLabels[surface] || surface,
      status: isRequested ? coverageStatusLabel(typeof status === 'string' ? status : null) : '未请求',
    }
  })
}

function sourceWasEditedAfterPosting(item: ThemeLeadItem) {
  if (!item.posted_at || !item.source_updated_at) return false
  const posted = Date.parse(item.posted_at)
  const updated = Date.parse(item.source_updated_at)
  return Number.isFinite(posted) && Number.isFinite(updated) && updated > posted
}

function readableProviderWarning(value: string) {
  const parts = value.split(/[;\n]+/).map((part) => part.trim()).filter(Boolean)
  const hasDetailedSurfaceWarning = parts.some((part) => part.startsWith('surface:'))
  const readable = parts
    .filter((part) => !(part === 'coverage:partial' && hasDetailedSurfaceWarning))
    .map((part) => {
      const surfaceMatch = part.match(/^surface:([^:]+):(.+)$/)
      if (surfaceMatch) {
        const surface = providerSurfaceLabels[surfaceMatch[1]] || surfaceMatch[1]
        const reason = surfaceMatch[2]
        if (reason === 'partial') return `${surface}采集部分缺失`
        if (reason === 'failed') return `${surface}采集失败`
        if (reason === 'row_missing_id_text_or_question') return `${surface}中部分记录缺少标识或正文，已保留有效记录`
        return `${surface}采集：${reason}`
      }
      if (part === 'coverage:partial') return '采集范围部分缺失'
      if (part === 'coverage:failed') return '采集范围失败'
      return providerWarningLabels[part] || `采集提示：${part}`
    })
  return Array.from(new Set(readable)).join('；')
}

function SymbolBadges({ symbols }: { symbols: ThemeLeadMappedSymbol[] }) {
  if (!symbols.length) return <span className="secondary-line">未映射证券</span>
  return <div className="theme-symbols">{symbols.map((symbol) => <span className="badge blue" key={`${symbol.symbol}-${symbol.name}`} title="仅研究关联，不产生审批或交易动作">{symbolLabel(symbol)} · 研究关联</span>)}</div>
}

function ThemeCard({ summary, selected, onSelect }: { summary: ThemeLeadSummary; selected: boolean; onSelect: () => void }) {
  const sourceRole = summary.first_source_role || 'original'
  const researchSummary = summary.first_research_posted_at !== undefined
  const originalLabel = researchSummary ? '最早原创讨论' : '最早原创'
  const firstOriginalDate = researchSummary ? summary.first_research_evidence_at || summary.first_research_posted_at : summary.first_original_evidence_at || summary.first_original_posted_at || (sourceRole === 'original' ? summary.first_posted_at : null)
  const firstOriginalAuthor = researchSummary ? summary.first_research_author_name : summary.first_original_author_name || (sourceRole === 'original' ? summary.first_author_name : null)
  const firstOriginalPlatform = researchSummary ? summary.first_research_platform : summary.first_original_platform || (sourceRole === 'original' ? summary.first_platform : null)
  const firstOriginalUrl = researchSummary ? summary.first_research_url : summary.first_original_url || (sourceRole === 'original' ? summary.first_url : null)
  return <article className={`theme-card ${selected ? 'selected' : ''}`}>
    <button className="theme-card-select" aria-pressed={selected} onClick={onSelect}>
      <div className="theme-card-heading"><span className="eyebrow">主题</span><strong>{summary.theme_name || summary.theme_id}</strong><span className="theme-card-count">{summary.post_count} 条</span></div>
      <div className="theme-card-facts">
        <div><small>库内最早命中</small><strong>{safeDate(summary.first_evidence_at || summary.first_posted_at)}</strong></div>
        <div><small>{originalLabel}</small><strong>{safeDate(firstOriginalDate)}</strong></div>
        <div><small>实际首次检出</small><strong>{safeDate(summary.first_detected_at)}</strong></div>
        <div><small>研究讨论</small><strong>{summary.research_count ?? '—'}</strong></div>
      </div>
      <div className="theme-card-foot"><span>记录的源采集时间 {safeDate(summary.first_source_fetched_at)}</span><span>原始来源 {summary.source_count} · 二手帖子 {summary.secondhand_post_count}</span><span>最近原帖 {safeDate(summary.last_posted_at)}</span></div>
      <SymbolBadges symbols={summary.mapped_symbols || []} />
    </button>
    <div className="theme-card-origin">
      <div className="theme-card-origin-details">
        <span>最早命中：{summary.first_author_name || '未标注'}{summary.first_platform ? ` · ${summary.first_platform}` : ''} <span className="badge neutral">{sourceRoleLabels[sourceRole as ThemeLeadSourceRole] || sourceRole}</span></span>
        <span>{originalLabel}：{firstOriginalAuthor || (researchSummary ? '暂无' : '未标注')}{firstOriginalPlatform ? ` · ${firstOriginalPlatform}` : ''}</span>
      </div>
      <div className="theme-card-origin-links">
        {summary.first_url ? <a href={summary.first_url} target="_blank" rel="noreferrer">打开最早命中 <ExternalLink size={12} /></a> : <span>暂无最早命中链接</span>}
        {firstOriginalUrl ? <a href={firstOriginalUrl} target="_blank" rel="noreferrer">打开{originalLabel} <ExternalLink size={12} /></a> : <span>暂无{originalLabel}链接</span>}
      </div>
    </div>
  </article>
}

function EvidenceDetails({ item }: { item: ThemeLeadItem }) {
  const body = itemText(item)
  const questionTitle = item.evidence?.some((evidence) => evidence.field === 'article_title' && evidence.context_role === 'question')
  return <details className="theme-source-evidence">
    <summary>查看原文与字段证据{item.evidence?.length ? ` · ${item.evidence.length} 处` : ''}</summary>
    <div className="theme-source-evidence-body">
      <p className="theme-original-text">{body}</p>
      <SourceVersions postId={item.post_id} />
      {!!item.article_title && <div className="theme-field"><span>{questionTitle ? '问题标题（不是回答作者观点）' : '文章标题（article_title）'}</span><p>{item.article_title}</p></div>}
      {!!item.article_text && item.article_text !== body && <div className="theme-field"><span>文章正文（article_text）</span><p>{item.article_text}</p></div>}
      {!!item.quoted_text && item.quoted_text !== body && <div className="theme-field"><span>引用正文（quoted_text）</span><p>{item.quoted_text}</p></div>}
      {!!item.ocr_text && item.ocr_text !== body && <div className="theme-field"><span>图片文字（ocr_text）</span><p>{item.ocr_text}</p></div>}
      {!!item.evidence?.length && <div className="theme-evidence-tags">{item.evidence.map((evidence, index) => <span className="tag" key={`${evidence.field}-${evidence.start}-${index}`} title={`${evidence.field} ${evidence.start}-${evidence.end}`}><b>{evidenceFieldLabel(evidence.field, evidence.context_role)}</b> · {evidence.matched_term || evidence.text}</span>)}</div>}
    </div>
  </details>
}

function SourceCoverageStatus({ coverage }: { coverage?: ThemeLeadSourceCoverage | null }) {
  const statuses = coverageStatuses(coverage)
  if (!statuses.length) return null
  return <div className="theme-source-coverage" aria-label="采集范围状态">
    <span>采集范围</span>
    {statuses.map(({ surface, label, status }) => <span className="badge neutral" key={surface}>{label}：{status}</span>)}
  </div>
}

function SourceTimelineItem({ item }: { item: ThemeLeadItem }) {
  const actualAuthor = Boolean(item.author_name && item.author_name !== item.display_name)
  const role = item.source_role || 'original'
  const surface = sourceSurfaceKey(item)
  return <article className={`theme-source-item role-${role}`}>
    <div className="theme-source-marker" aria-hidden="true" />
    <div className="theme-source-main">
      <div className="theme-source-heading">
        <div className="theme-source-title"><span className={`badge ${role === 'original' ? 'green' : role === 'aggregation' || role === 'secondhand' ? 'amber' : 'neutral'}`}>{sourceRoleLabels[role] || role}</span><span className="badge neutral">{kindLabels[item.kind] || item.kind || '主题证据'}</span>{surface && <span className="badge neutral">来源面：{sourceSurfaceLabel(surface)}</span>}<strong>{item.theme_name}</strong></div>
        <a className="theme-source-link" href={item.url} target="_blank" rel="noreferrer">原帖 <ExternalLink size={13} /></a>
      </div>
      <div className="theme-source-author"><strong>{item.display_name || item.handle || '未署名账号'}</strong>{actualAuthor ? <span>实际原作者：{item.author_name}</span> : item.author_name ? <span>{item.author_name}</span> : null}{item.handle && <span className="mono">@{item.handle}</span>}{item.platform && <span className="badge neutral">{item.platform}</span>}</div>
      {item.quoted_author && <div className="theme-source-note">引用作者：{item.quoted_author}</div>}
      <div className="theme-source-times"><span>原文发布 {safeDate(item.posted_at)}</span><span>实际检出 {safeDate(item.first_detected_at)}</span><span>抓取 {safeDate(item.fetched_at)}</span></div>
      <SourceCoverageStatus coverage={item.source_coverage} />
      {!!item.matched_terms?.length && <div className="theme-evidence-tags">{item.matched_terms.map((term) => <span className="tag" key={term}>{term}</span>)}</div>}
      {!!item.claimed_timing?.length && <div className="theme-source-note">原文追述时间：{item.claimed_timing.join(' · ')}（仅按文字保留，不改写原文发布时间）</div>}
      {item.provider_warning && <div className="theme-provider-warning" title={item.provider_warning}><ShieldAlert size={14} /><span>{readableProviderWarning(item.provider_warning)}</span></div>}
      {sourceWasEditedAfterPosting(item) && <div className="theme-provider-warning"><ShieldAlert size={14} /><span>原帖有后续编辑，当前文本不能证明主题在初次发帖时已出现</span></div>}
      <EvidenceDetails item={item} />
      <SymbolBadges symbols={item.mapped_symbols || []} />
    </div>
  </article>
}

export default function ThemeRadar() {
  const { params } = useRoute()
  const queryClient = useQueryClient()
  const q = params.get('q') || ''
  const themeId = params.get('theme_id') || ''
  const kind = params.get('kind') || ''
  const kolId = params.get('kol_id') || ''
  const dateFrom = params.get('date_from') || ''
  const dateTo = params.get('date_to') || ''
  const sort = params.get('sort') === 'oldest' ? 'oldest' : 'newest'
  const page = Math.max(1, Number(params.get('page')) || 1)
  const [debouncedQ, setDebouncedQ] = useState(q)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQ(q), 250)
    return () => window.clearTimeout(timer)
  }, [q])
  const queryParams = useMemo(() => {
    const value = new URLSearchParams({ page: String(page), page_size: '50', sort })
    if (debouncedQ) value.set('q', debouncedQ)
    if (themeId) value.set('theme_id', themeId)
    if (kind) value.set('kind', kind)
    if (/^\d+$/.test(kolId) && Number(kolId) > 0) value.set('kol_id', kolId)
    if (dateFrom) value.set('date_from', dateFrom)
    if (dateTo) value.set('date_to', dateTo)
    return value
  }, [debouncedQ, themeId, kind, kolId, dateFrom, dateTo, page, sort])
  const query = useQuery({ queryKey: ['theme-leads', queryParams.toString()], queryFn: ({ signal }) => api.themeLeads(queryParams, signal), refetchInterval: 30_000 })
  const items = useMemo(() => [...(query.data?.items || [])].sort((left, right) => {
    const leftDate = left.posted_at ? Date.parse(left.posted_at) : 0
    const rightDate = right.posted_at ? Date.parse(right.posted_at) : 0
    return sort === 'oldest' ? leftDate - rightDate : rightDate - leftDate
  }), [query.data?.items, sort])
  const [toast, setToast] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  const startedAt = useRef(0)
  const extract = useMutation({
    mutationFn: () => api.extractThemeLeads(),
    onMutate: () => { startedAt.current = performance.now(); setToast(null) },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['theme-leads'] })
      const readTime = Number.isFinite(result.read_time_ms) ? result.read_time_ms : Math.round(performance.now() - startedAt.current)
      setToast({ kind: 'success', text: `本地证据回放完成 · 读取耗时 ${readTime} ms` })
    },
    onError: (error) => setToast({ kind: 'error', text: `本地证据回放失败 · ${error instanceof Error ? error.message : '请稍后重试'}` }),
  })

  const setFilter = (key: string, value: string | null) => updateRoute({ [key]: value, page: 1 })
  const clearFilters = () => updateRoute({ q: null, theme_id: null, kind: null, kol_id: null, date_from: null, date_to: null, sort: null, page: 1 })
  const summaries = query.data?.summary || []
  const totalPages = query.data?.total_pages || 1

  return <section className="theme-radar-workspace">
    <header className="page-header theme-radar-header"><div><span className="eyebrow">LOCAL THEME EVIDENCE</span><h1>主题雷达</h1><p className="page-description">按主题查看原文时间线，区分库内最早命中、最早原创讨论、实际检出时间与来源角色。</p></div><div className="header-actions"><button className="secondary-button" disabled={extract.isPending} onClick={() => extract.mutate()}><RefreshCw size={15} className={extract.isPending ? 'spin' : ''} />{extract.isPending ? '正在回放…' : '回放本地证据'}</button></div></header>
    <div className="theme-radar-warning"><ShieldAlert size={16} /><span>知乎采集范围存在缺口，无法证明全账号无提及；请核对回答/文章/想法覆盖；历史仅回答。</span></div>
    <TopicDiscovery />
    <div className="theme-radar-toolbar panel"><label className="theme-search"><Search size={16} /><input aria-label="搜索主题、作者或原文" value={q} onChange={(event) => setFilter('q', event.target.value)} placeholder="搜索会稽山、作者、主题或原文" /></label><label><span>主题</span><select aria-label="筛选主题" value={themeId} onChange={(event) => setFilter('theme_id', event.target.value)}><option value="">全部主题</option>{summaries.map((summary) => <option key={summary.theme_id} value={summary.theme_id}>{summary.theme_name}</option>)}</select></label><label><span>种类</span><select aria-label="筛选证据种类" value={kind} onChange={(event) => setFilter('kind', event.target.value)}><option value="">全部种类</option>{kindOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label><label><span>KOL ID</span><input aria-label="筛选 KOL ID" inputMode="numeric" value={kolId} onChange={(event) => setFilter('kol_id', event.target.value)} placeholder="全部" /></label><label><span>从</span><input aria-label="主题证据开始日期" type="date" value={dateFrom} onChange={(event) => setFilter('date_from', event.target.value)} /></label><label><span>到</span><input aria-label="主题证据结束日期" type="date" value={dateTo} onChange={(event) => setFilter('date_to', event.target.value)} /></label><label><span>时间</span><select aria-label="主题时间顺序" value={sort} onChange={(event) => setFilter('sort', event.target.value)}><option value="newest">最近优先</option><option value="oldest">最早优先</option></select></label><button className="icon-button" title="清除主题筛选" onClick={clearFilters}><History size={16} /></button></div>
    <QueryState loading={query.isLoading} error={query.error} stale={Boolean(query.data)} />
    <div className="theme-radar-context"><span><Radar size={15} />{query.data ? `当前返回 ${query.data.total} 条证据 · 第 ${query.data.page} / ${totalPages} 页` : '正在读取主题证据'}</span><span>汇总覆盖当前主题/关键词/种类/KOL筛选下的全部历史；日期只过滤时间线。</span></div>
    {!!summaries.length && <div className="theme-card-grid" aria-label="主题汇总">{summaries.map((summary) => <ThemeCard key={summary.theme_id} summary={summary} selected={summary.theme_id === themeId} onSelect={() => setFilter('theme_id', summary.theme_id === themeId ? '' : summary.theme_id)} />)}</div>}
    {!query.isLoading && !summaries.length && <div className="empty-state theme-empty"><strong>没有词典命中的库内证据</strong><span>当前仅检索已配置的研究主题；可以换一个主题、作者或原文关键词。</span></div>}
    <div className="theme-timeline-panel panel"><div className="panel-heading"><div><h2>原文时间线</h2><span>{sort === 'oldest' ? '最早优先' : '最近优先'} · {items.length} 条当前页证据</span></div></div><div className="theme-timeline">{items.map((item) => <SourceTimelineItem key={`${item.id}-${item.post_id}`} item={item} />)}{!query.isLoading && !items.length && <div className="empty-state"><strong>当前筛选没有原文时间线记录</strong><span>主题汇总仍按全历史计算，日期条件只影响这里的帖子。</span></div>}</div>{totalPages > 1 && <footer className="theme-pagination"><button className="secondary-button" disabled={page <= 1} onClick={() => updateRoute({ page: page - 1 })}>上一页</button><span>第 {page} / {totalPages} 页</span><button className="secondary-button" disabled={page >= totalPages} onClick={() => updateRoute({ page: page + 1 })}>下一页</button></footer>}</div>
    {toast && <div className={`action-toast ${toast.kind}`} role="status"><span>{toast.text}</span><button className="icon-button" title="关闭提示" onClick={() => setToast(null)}>×</button></div>}
  </section>
}

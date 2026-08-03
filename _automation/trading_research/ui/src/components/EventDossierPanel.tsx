import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Download, RefreshCw } from 'lucide-react'
import { api, percent } from '../api'
import type { EventDossierSection } from '../types'
import EventMethodResearch from './EventMethodResearch'

const sectionLabels: Record<string, string> = {
  recommendation: 'Recommendation',
  minute_data: 'Minute data',
  evidence: '原帖与证据',
  event_market: '推荐时行情',
  technical: '技术环境',
  intraday: '分钟窗口',
  fundamentals: '财务与辅助数据',
  board_context: '板块归属',
  performance: '收益与节点',
}

function statusLabel(status: string): string {
  return ({ ready: '完整', partial: '部分可用', pending: '待补齐', unavailable: '暂无数据', failed: '失败' } as Record<string, string>)[status] || status
}

function statusClass(status: string): string {
  if (status === 'ready') return 'green'
  if (status === 'failed') return 'red'
  if (status === 'partial' || status === 'pending') return 'amber'
  return 'neutral'
}

function preview(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) : '-'
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

function marketFacts(section: EventDossierSection) {
  const raw = section.raw_at_event as Record<string, unknown> | null | undefined
  const qfq = section.qfq_at_event as Record<string, unknown> | null | undefined
  const source = raw || qfq
  if (!source) return <div className="empty-state compact">推荐时没有可用日线</div>
  const close = Number(source.close)
  const preclose = Number(source.preclose)
  const high = Number(source.high)
  const low = Number(source.low)
  const change = Number.isFinite(close) && Number.isFinite(preclose) && preclose !== 0 ? close / preclose - 1 : null
  const amplitude = Number.isFinite(high) && Number.isFinite(low) && Number.isFinite(preclose) && preclose !== 0 ? (high - low) / preclose : null
  const fields: Array<[string, string]> = [
    ['有效交易日', preview(section.effective_trade_date)],
    ['收盘价', preview(source.close)],
    ['涨跌幅', change === null ? '-' : percent(change)],
    ['振幅', amplitude === null ? '-' : percent(amplitude)],
    ['成交量', preview(source.volume)],
    ['成交额', preview(source.amount)],
    ['换手率', preview(source.turnover)],
    ['供应商', preview(source.provider)],
  ]
  return <div className="dossier-fact-grid">{fields.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
}

function evidenceBody(section: EventDossierSection) {
  const post = section.post as Record<string, unknown> | null | undefined
  const media = Array.isArray(post?.local_media)
    ? post.local_media as Array<Record<string, unknown>>
    : []
  const quotedText = post?.quoted_text || post?.quoted_content
  const ocrText = post?.ocr_text
  return <div className="dossier-evidence">
    <div className="dossier-text-block">
      <p>{preview(post?.text || post?.article_text)}</p>
      <small>Source: {preview(post?.url || post?.source_url)}</small>
    </div>
    {!!quotedText && <details className="dossier-json" open={false}>
      <summary>Quoted context{post?.quoted_author ? ` · ${preview(post.quoted_author)}` : ''}</summary>
      <pre>{preview(quotedText)}</pre>
    </details>}
    {!!ocrText && <details className="dossier-json" open={false}>
      <summary>OCR snapshot</summary>
      <pre>{preview(ocrText)}</pre>
    </details>}
    {media.length > 0 && <div className="dossier-media-grid">
      {media.map((item, index) => <a key={String(item.api_url || index)} href={String(item.api_url || '#')} target="_blank" rel="noreferrer">
        <img src={String(item.api_url || item.path || '')} alt={`post evidence ${index + 1}`} loading="lazy" />
      </a>)}
    </div>}
  </div>
}

function renderSectionBody(key: string, section: EventDossierSection) {
  if (key === 'event_market') return marketFacts(section)
  if (key === 'recommendation') {
    const drafts = Array.isArray(section.drafts) ? section.drafts as Array<Record<string, unknown>> : []
    return <div className="dossier-recommendations">{drafts.map((draft, index) => <article key={String(draft.id || index)}><strong>{preview(draft.security_name || draft.symbol)}</strong><span>{preview(draft.direction)} · {preview(draft.action)} · {preview(draft.horizon)} · {preview(draft.strength)}</span><p>{preview(draft.thesis)}</p><small>证据：{preview(draft.evidence_spans)}</small></article>)}{drafts.length === 0 && <div className="dossier-text-block">{preview(section.data)}</div>}</div>
  }
  if (key === 'performance') {
    const summary = (section.summary || {}) as Record<string, unknown>
    const checkpoints = Array.isArray(section.checkpoints) ? section.checkpoints as Array<Record<string, unknown>> : []
    return <>
      <div className="dossier-fact-grid dossier-fact-grid-small">
        <div><span>收益快照</span><strong>{preview(summary.mark_count || 0)}</strong></div>
        <div><span>数据范围</span><strong>{preview(summary.available_from)} - {preview(summary.available_to)}</strong></div>
        <div><span>冻结节点</span><strong>{checkpoints.length}</strong></div>
      </div>
      <details className="dossier-json"><summary>查看最新收益与冻结节点</summary><pre>{preview({ latest_mark: summary.latest_mark, checkpoints })}</pre></details>
    </>
  }
  if (key === 'technical') {
    const data = section.data as Record<string, unknown> | null | undefined
    if (!data) return <div className="empty-state compact">技术环境尚未生成</div>
    const fields = ['as_of_trade_date', 'rsi14', 'macd_dif', 'macd_dea', 'macd_hist', 'atr14_pct', 'volume_ratio_5', 'return_20d', 'distance_60d_high']
    return <div className="dossier-fact-grid">{fields.map((field) => <div key={field}><span>{field}</span><strong>{preview(data[field])}</strong></div>)}</div>
  }
  if (key === 'evidence') return evidenceBody(section)
  if (key === 'legacy_evidence') {
    const post = section.post as Record<string, unknown> | null | undefined
    return <div className="dossier-text-block"><p>{preview(post?.text || post?.article_text || post?.ocr_text)}</p><small>来源：{preview(post?.url || post?.source_url)}</small></div>
  }
  if (key === 'intraday') {
    const data = section.data as Record<string, unknown> | null | undefined
    return <details className="dossier-json" open={false}><summary>查看分钟窗口摘要</summary><pre>{preview(data)}</pre></details>
  }
  const data = section.datasets || section.rows || section.data
  return <details className="dossier-json"><summary>查看结构化数据</summary><pre>{preview(data)}</pre></details>
}

export default function EventDossierPanel({ eventId }: { eventId: string }) {
  const client = useQueryClient()
  const dossier = useQuery({ queryKey: ['event-dossier', eventId], queryFn: () => api.eventDossier(eventId) })
  const refresh = useMutation({
    mutationFn: () => api.refreshEventDossier(eventId),
    onSuccess: () => client.invalidateQueries({ queryKey: ['event-dossier', eventId] }),
  })
  if (dossier.isLoading) return <section className="event-dossier-panel"><div className="section-heading"><h3>证据与数据档案</h3></div><div className="loading-state">正在加载事件档案...</div></section>
  if (dossier.isError || !dossier.data) return <section className="event-dossier-panel"><div className="section-heading"><h3>证据与数据档案</h3></div><div className="error-banner">档案读取失败：{String(dossier.error)}</div></section>
  const value = dossier.data
  const sections = value.sections || {}
  const completeness = value.completeness || { ready: 0, total: 0 }
  const warnings = Array.isArray(value.warnings) ? value.warnings : []
  return <section className="event-dossier-panel">
    <div className="section-heading dossier-heading"><div><span className="eyebrow">AUDIT DOSSIER</span><h3>证据与数据档案</h3><small>{completeness.ready}/{completeness.total} 个分区可用 · {value.status || 'pending'}</small></div><div className="header-actions"><a className="icon-button" href={api.exportEventDossier(eventId)} target="_blank" rel="noreferrer" title="导出档案"><Download size={15} /></a><button className="secondary-button" disabled={refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw size={15} className={refresh.isPending ? 'spin' : ''} />刷新数据</button></div></div>
    {warnings.length > 0 && <div className="warning-banner"><AlertTriangle size={15} /><span>{warnings.join(' · ')}</span></div>}
    <div className="dossier-sections">{Object.entries(sections).map(([key, section]) => <details key={key} className="dossier-section" open={key === 'evidence' || key === 'event_market'}><summary><span>{sectionLabels[key] || key}</span><span className={`badge ${statusClass(section.status)}`}>{statusLabel(section.status)}</span></summary><div className="dossier-section-body">{renderSectionBody(key, section)}{section.warnings && section.warnings.length > 0 && <small className="dossier-warning">{section.warnings.join(' · ')}</small>}</div></details>)}</div>
    <EventMethodResearch eventId={eventId} />
    {refresh.isError && <div className="error-banner">刷新失败：{String(refresh.error)}</div>}
  </section>
}

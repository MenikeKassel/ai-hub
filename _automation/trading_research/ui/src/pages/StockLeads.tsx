import { useDeferredValue, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, ExternalLink, Search, X } from 'lucide-react'
import { api, formatDate } from '../api'
import type { StockLead } from '../types'

function isoDaysAgo(days: number): string {
  const value = new Date()
  value.setDate(value.getDate() - days)
  return value.toLocaleDateString('en-CA')
}

const kindLabels: Record<string, string> = {
  recommendation: '推荐', analysis: '分析', holding: '持仓', retrospective: '复盘', secondhand: '二手转述',
}

export default function StockLeads() {
  const client = useQueryClient()
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const [kind, setKind] = useState('')
  const [symbol, setSymbol] = useState('')
  const [dateFrom, setDateFrom] = useState(isoDaysAgo(30))
  const [dateTo, setDateTo] = useState(new Date().toLocaleDateString('en-CA'))
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<StockLead | null>(null)
  const reprocess = useMutation({
    mutationFn: (postId: string) => api.reprocessRecommendationPost(postId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['stock-mentions'] })
      client.invalidateQueries({ queryKey: ['morning-review'] })
    },
  })
  useEffect(() => setPage(1), [deferredQuery, kind, symbol, dateFrom, dateTo])
  const params = new URLSearchParams({ page: String(page), page_size: '50' })
  if (deferredQuery.trim()) params.set('q', deferredQuery.trim())
  if (kind) params.set('kind', kind)
  if (/^\d{6}$/.test(symbol)) params.set('symbol', symbol)
  if (dateFrom) params.set('date_from', dateFrom)
  if (dateTo) params.set('date_to', dateTo)
  const mentions = useQuery({ queryKey: ['stock-mentions', params.toString()], queryFn: () => api.stockMentions(params), placeholderData: (previous) => previous })
  const result = mentions.data

  return <section>
    <header className="page-header"><div><span className="eyebrow">SEARCHABLE ARCHIVE</span><h1>历史归档</h1><p className="page-subtitle">股票提及仅供检索，不再作为人工待办。默认显示最近 30 天。</p></div></header>
    <div className="archive-toolbar archive-filter-grid">
      <label className="search-field"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索股票、KOL或原文" /></label>
      <input aria-label="股票代码筛选" value={symbol} onChange={(event) => setSymbol(event.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="股票代码" />
      <select aria-label="提及类型" value={kind} onChange={(event) => setKind(event.target.value)}><option value="">全部类型</option>{Object.entries(kindLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
      <input aria-label="起始日期" type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
      <input aria-label="结束日期" type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
      <span>{result?.total ?? '-'} 条</span>
    </div>
    {mentions.isError && <div className="error-banner">历史归档读取失败：{String(mentions.error)}</div>}
    <div className={`archive-workbench ${selected ? 'with-detail' : ''}`}>
      <div className="panel mention-index">
        <div className="archive-table-scroll"><table><thead><tr><th>时间</th><th>KOL</th><th>股票</th><th>类型</th><th>证据</th><th>来源</th></tr></thead><tbody>
          {(result?.items || []).map((lead) => <tr className={selected?.id === lead.id ? 'selected' : ''} key={lead.id} onClick={() => setSelected(lead)}>
            <td>{formatDate(lead.posted_at)}</td><td>@{lead.handle}</td><td><strong>{lead.symbol}</strong><small>{lead.security_name}</small></td><td>{kindLabels[lead.mention_kind] || lead.mention_kind}</td><td><p className="mention-evidence">{lead.evidence_text || lead.text}</p></td><td><a className="icon-link" href={lead.url} target="_blank" rel="noreferrer" title="打开原帖" onClick={(event) => event.stopPropagation()}><ExternalLink size={15} /></a></td>
          </tr>)}
        </tbody></table></div>
        {!mentions.isLoading && !result?.items.length && <div className="empty-state compact">没有匹配的历史提及</div>}
        <div className="archive-pagination"><span>第 {result?.page || page} / {result?.total_pages || 1} 页 · 每页 50 条</span><div><button className="icon-button" title="上一页" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}><ChevronLeft size={16} /></button><button className="icon-button" title="下一页" disabled={!result || page >= result.total_pages} onClick={() => setPage((value) => value + 1)}><ChevronRight size={16} /></button></div></div>
      </div>
      {selected && <aside className="archive-detail">
        <div className="source-heading"><div><span className="eyebrow">MENTION #{selected.id}</span><h2>{selected.symbol} {selected.security_name}</h2><small>@{selected.handle} · {formatDate(selected.posted_at)}</small></div><button className="icon-button" title="关闭详情" onClick={() => setSelected(null)}><X size={16} /></button></div>
        <dl className="event-readonly"><div><dt>提及类型</dt><dd>{kindLabels[selected.mention_kind] || selected.mention_kind}</dd></div><div><dt>提取方式</dt><dd>{selected.extraction_method}</dd></div><div><dt>关联事件</dt><dd>{selected.related_event_ids?.join('、') || '没有正式事件'}</dd></div></dl>
        <section className="archive-source-block"><span>证据</span><p>{selected.evidence_text || '-'}</p></section>
        <section className="archive-source-block"><span>原帖正文</span><p>{selected.text || '-'}</p></section>
        {selected.article_text && <section className="archive-source-block"><span>长文正文</span><p>{selected.article_text}</p></section>}
        {selected.ocr_text && <section className="archive-source-block"><span>OCR</span><pre>{selected.ocr_text}</pre></section>}
        <div className="header-actions archive-source-actions"><a className="secondary-button archive-source-link" href={selected.url} target="_blank" rel="noreferrer"><ExternalLink size={15} />打开原始来源</a><button className="secondary-button" disabled={reprocess.isPending} onClick={() => reprocess.mutate(selected.post_id)}>重新生成推荐草稿</button></div>
        {reprocess.isSuccess && <div className="success-banner">已重新生成推荐草稿，请到今日审核查看。</div>}
        {reprocess.isError && <div className="error-banner">推荐草稿重新生成失败：{String(reprocess.error)}</div>}
      </aside>}
    </div>
  </section>
}

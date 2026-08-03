import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, ExternalLink, History, Pencil, RotateCcw, Search, TrendingUp, X } from 'lucide-react'
import { api, formatDate } from '../api'
import ActionToast from '../components/ActionToast'
import EventDossierPanel from '../components/EventDossierPanel'
import type { Event, EventAmendment, EventUpdate } from '../types'

type EventStatus = 'active' | 'completed' | 'candidate' | 'excluded' | 'archived'
type EventFilter = EventStatus | 'waiting'

const labels: Record<EventFilter, string> = {
  active: '跟踪中', waiting: '等待行情', completed: '已完成', candidate: '候选', excluded: '已排除', archived: '已归档',
}

function eventTone(status: string): string {
  if (status === 'active' || status === 'completed') return 'green'
  if (status === 'candidate') return 'amber'
  if (status === 'excluded') return 'red'
  return 'neutral'
}

function isWaiting(event: Event): boolean {
  return event.status === 'active' && (!event.baseline_date || !event.latest_mark)
}

export default function EventManagement() {
  const client = useQueryClient()
  const events = useQuery({ queryKey: ['events'], queryFn: api.events })
  const [filter, setFilter] = useState<EventFilter>('active')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const values = events.data || []
  const counts = useMemo<Record<EventFilter, number>>(() => ({
    active: values.filter((event) => event.status === 'active').length,
    waiting: values.filter(isWaiting).length,
    completed: values.filter((event) => event.status === 'completed').length,
    candidate: values.filter((event) => event.status === 'candidate').length,
    excluded: values.filter((event) => event.status === 'excluded').length,
    archived: values.filter((event) => event.status === 'archived').length,
  }), [values])
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return values
      .filter((event) => filter === 'waiting' ? isWaiting(event) : event.status === filter)
      .filter((event) => !query || [event.event_id, event.symbol, event.security_name, event.kol_name, event.thesis]
        .some((value) => value.toLowerCase().includes(query)))
      .sort((a, b) => b.posted_at.localeCompare(a.posted_at))
  }, [filter, search, values])
  const selected = filtered.find((event) => event.event_id === selectedId) || filtered[0]
  useEffect(() => { if (selectedId && !filtered.some((event) => event.event_id === selectedId)) setSelectedId('') }, [filtered, selectedId])
  const mutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: EventUpdate }) => api.patchEvent(id, value),
    onSuccess: () => { setSelectedId(''); client.invalidateQueries({ queryKey: ['events'] }); client.invalidateQueries({ queryKey: ['market'] }) },
  })
  const amendment = useMutation({
    mutationFn: ({ id, value }: { id: string; value: EventAmendment }) => api.amendEvent(id, value),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['events'] }); client.invalidateQueries({ queryKey: ['market'] }); client.invalidateQueries({ queryKey: ['event-revisions'] }) },
  })
  const message = amendment.isPending ? '正在保存修订并评估收益重算…'
    : amendment.isSuccess ? amendment.data.refresh_status === 'queued' ? '事件已修订，行情补齐与收益重算已入队' : '事件已修订，现有收益保持不变'
      : amendment.isError ? String(amendment.error)
        : mutation.isPending ? '正在保存事件状态…' : mutation.isSuccess ? '事件状态已更新' : mutation.isError ? String(mutation.error) : ''

  return <section>
    <header className="page-header"><div><span className="eyebrow">FORMAL EVENTS</span><h1>正式事件</h1><p className="page-subtitle">这里只管理你已经批准的独立推荐证据、行情基准与跟踪状态。</p></div></header>
    <div className="event-summary" aria-label="正式事件概览">
      {(['active', 'waiting', 'completed', 'candidate'] as EventFilter[]).map((value) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => { setFilter(value); setSelectedId('') }}><span>{labels[value]}</span><strong>{counts[value]}</strong></button>)}
    </div>
    <div className="event-manager-toolbar">
      <div className="segmented" role="tablist">{(['active', 'waiting', 'completed', 'candidate', 'excluded', 'archived'] as EventFilter[]).map((value) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => { setFilter(value); setSelectedId('') }}>{labels[value]} {counts[value]}</button>)}</div>
      <label className="search-field event-search"><Search size={15} /><input aria-label="搜索正式事件" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="事件、股票或 KOL" /></label>
      <span>显示 {filtered.length} 条</span>
    </div>
    {events.isError && <div className="error-banner">事件读取失败：{String(events.error)}</div>}
    <div className="event-manager">
      <aside className="event-manager-list">{filtered.map((event) => <button className={selected?.event_id === event.event_id ? 'event-manager-item active' : 'event-manager-item'} key={event.event_id} onClick={() => setSelectedId(event.event_id)}>
        <div><span className="mono">{event.symbol || event.event_id}</span><span className={`badge ${eventTone(event.status)}`}>{isWaiting(event) ? '等待行情' : labels[event.status as EventStatus] || event.status}</span></div>
        <strong>{event.security_name || '标的待补'}</strong>
        <small>{event.kol_name} · {formatDate(event.posted_at).split(' ')[0]}</small>
        <p>{event.exclusion_reason || event.thesis || '尚未填写推荐理由'}</p>
      </button>)}{!events.isLoading && !filtered.length && <div className="empty-state">没有符合当前筛选的事件</div>}</aside>
      <main className="event-manager-detail">{selected ? <EventEditor event={selected} busy={mutation.isPending || amendment.isPending} onUpdate={(value) => mutation.mutate({ id: selected.event_id, value })} onAmend={(value) => amendment.mutate({ id: selected.event_id, value })} /> : <div className="empty-state">选择一条事件查看详情</div>}</main>
    </div>
    {message && <ActionToast state={mutation.isPending || amendment.isPending ? 'pending' : mutation.isError || amendment.isError ? 'error' : 'success'} message={message} />}
  </section>
}

function EventEditor({ event, busy, onUpdate, onAmend }: { event: Event; busy: boolean; onUpdate: (value: EventUpdate) => void; onAmend: (value: EventAmendment) => void }) {
  const [form, setForm] = useState({ kol_name: event.kol_name, platform: event.platform || 'X', source_url: event.source_url, source_note: event.source_note, posted_at: event.posted_at ? event.posted_at.slice(0, 16) : '', symbol: event.symbol, security_name: event.security_name, direction: event.direction || 'long', thesis: event.thesis })
  const [reason, setReason] = useState(event.exclusion_reason || '')
  useEffect(() => { setForm({ kol_name: event.kol_name, platform: event.platform || 'X', source_url: event.source_url, source_note: event.source_note, posted_at: event.posted_at ? event.posted_at.slice(0, 16) : '', symbol: event.symbol, security_name: event.security_name, direction: event.direction || 'long', thesis: event.thesis }); setReason(event.exclusion_reason || '') }, [event])

  if (event.status === 'active' || event.status === 'completed') return <ActiveEventDetail event={event} busy={busy} onAmend={onAmend} />

  if (event.status !== 'candidate') return <>
    <div className="source-heading"><div><span className="eyebrow">{event.event_id}</span><h2>{event.symbol} {event.security_name}</h2><small>{event.kol_name} · {labels[event.status as EventStatus] || event.status}</small></div>{event.source_url && <a className="icon-button" href={event.source_url} target="_blank" rel="noreferrer" title="打开原帖"><ExternalLink size={16} /></a>}</div>
    <section className="event-thesis"><span>推荐理由</span><p>{event.thesis || '-'}</p></section>
    <dl className="event-readonly"><div><dt>当前状态</dt><dd>{labels[event.status as EventStatus] || event.status}</dd></div><div><dt>处理原因</dt><dd>{event.exclusion_reason || '未记录原因'}</dd></div></dl>
    <div className="review-actions"><button className="secondary-button" disabled={busy} onClick={() => onUpdate({ action: 'restore' })}><RotateCcw size={16} />恢复为候选</button></div>
  </>

  const activate = (): EventUpdate => ({ action: 'activate', ...form, posted_at: form.posted_at ? new Date(form.posted_at).toISOString() : form.posted_at, direction: form.direction as 'long' | 'short' })
  return <>
    <div className="source-heading"><div><span className="eyebrow">{event.event_id}</span><h2>候选事件核对</h2><small>补齐六要素后才能进入正式收益跟踪</small></div>{event.source_url && <a className="icon-button" href={event.source_url} target="_blank" rel="noreferrer" title="打开原帖"><ExternalLink size={16} /></a>}</div>
    <div className="event-editor-grid">
      <label>KOL<input value={form.kol_name} onChange={(e) => setForm({ ...form, kol_name: e.target.value })} /></label><label>平台<input value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })} /></label><label>股票代码<input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value.replace(/\D/g, '').slice(0, 6) })} /></label><label>股票名称<input value={form.security_name} onChange={(e) => setForm({ ...form, security_name: e.target.value })} /></label><label>发布时间<input type="datetime-local" value={form.posted_at} onChange={(e) => setForm({ ...form, posted_at: e.target.value })} /></label><label>方向<select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as 'long' | 'short' })}><option value="long">看多</option><option value="short">看空</option></select></label><label className="wide">原始链接<input value={form.source_url} onChange={(e) => setForm({ ...form, source_url: e.target.value })} /></label><label className="wide">推荐理由<textarea value={form.thesis} onChange={(e) => setForm({ ...form, thesis: e.target.value })} /></label><label className="wide">排除原因<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
    </div>
    <div className="review-actions"><button className="danger-button" disabled={busy || !reason.trim()} onClick={() => onUpdate({ action: 'exclude', exclusion_reason: reason })}><X size={16} />排除</button><button className="primary-button" disabled={busy || !/^\d{6}$/.test(form.symbol) || !form.thesis.trim()} onClick={() => onUpdate(activate())}><Check size={16} />激活跟踪</button></div>
  </>
}

type EventDetailTab = 'overview' | 'baseline' | 'revisions'

function ActiveEventDetail({ event, busy, onAmend }: { event: Event; busy: boolean; onAmend: (value: EventAmendment) => void }) {
  const [tab, setTab] = useState<EventDetailTab>('overview')
  const [editing, setEditing] = useState(false)
  const [reason, setReason] = useState('')
  const [form, setForm] = useState({
    kol_name: event.kol_name, platform: event.platform, source_url: event.source_url,
    source_note: event.source_note, posted_at: event.posted_at.slice(0, 16), symbol: event.symbol,
    security_name: event.security_name, direction: event.direction, thesis: event.thesis,
  })
  useEffect(() => {
    setForm({ kol_name: event.kol_name, platform: event.platform, source_url: event.source_url, source_note: event.source_note, posted_at: event.posted_at.slice(0, 16), symbol: event.symbol, security_name: event.security_name, direction: event.direction, thesis: event.thesis })
    setEditing(false)
    setReason('')
  }, [event])
  const revisions = useQuery({ queryKey: ['event-revisions', event.event_id], queryFn: () => api.eventRevisions(event.event_id) })
  const normalizedPostedAt = form.posted_at ? new Date(form.posted_at).toISOString() : form.posted_at
  const changes: Omit<EventAmendment, 'reason'> = {}
  const compare: Record<keyof typeof form, string> = { ...form, posted_at: normalizedPostedAt }
  ;(Object.keys(form) as Array<keyof typeof form>).forEach((key) => {
    if (String(compare[key]) !== String(event[key])) Object.assign(changes, { [key]: compare[key] })
  })
  const changedFields = Object.keys(changes)
  const recalculates = changedFields.some((field) => ['symbol', 'direction', 'posted_at'].includes(field))
  const submit = () => {
    onAmend({ reason: reason.trim(), ...changes })
    setEditing(false)
  }

  return <>
    <div className="source-heading event-detail-heading">
      <div><span className="eyebrow">{event.event_id}</span><h2>{event.symbol} {event.security_name}</h2><small>{event.kol_name} · {formatDate(event.posted_at)}</small></div>
      <div className="header-actions"><button className="icon-button" title="纠错正式事件" onClick={() => setEditing(!editing)}><Pencil size={16} /></button><button className="secondary-button" onClick={() => { window.location.hash = '/backtests' }}><TrendingUp size={16} />查看收益</button>{event.source_url && <a className="icon-button" href={event.source_url} target="_blank" rel="noreferrer" title="打开原帖"><ExternalLink size={16} /></a>}</div>
    </div>
    <div className="event-detail-tabs segmented"><button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>事件概要</button><button className={tab === 'baseline' ? 'active' : ''} onClick={() => setTab('baseline')}>收益基准</button><button className={tab === 'revisions' ? 'active' : ''} onClick={() => setTab('revisions')}>修订记录 {revisions.data?.length || 0}</button></div>
    {editing && <section className="event-amendment-panel">
      <div className="section-heading"><div><span className="eyebrow">AUDITED AMENDMENT</span><h3>纠错正式事件</h3></div><button className="icon-button" title="关闭纠错" onClick={() => setEditing(false)}><X size={15} /></button></div>
      <div className="event-editor-grid">
        <label>KOL<input value={form.kol_name} onChange={(e) => setForm({ ...form, kol_name: e.target.value })} /></label><label>平台<input value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })} /></label>
        <label>股票代码<input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value.replace(/\D/g, '').slice(0, 6) })} /></label><label>股票名称<input value={form.security_name} onChange={(e) => setForm({ ...form, security_name: e.target.value })} /></label>
        <label>发布时间<input type="datetime-local" value={form.posted_at} onChange={(e) => setForm({ ...form, posted_at: e.target.value })} /></label><label>方向<select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as 'long' | 'short' })}><option value="long">看多</option><option value="short">看空</option></select></label>
        <label className="wide">原始链接<input value={form.source_url} onChange={(e) => setForm({ ...form, source_url: e.target.value })} /></label><label className="wide">来源说明<input value={form.source_note} onChange={(e) => setForm({ ...form, source_note: e.target.value })} /></label>
        <label className="wide">推荐理由<textarea value={form.thesis} onChange={(e) => setForm({ ...form, thesis: e.target.value })} /></label><label className="wide">修改原因<input aria-label="正式事件修改原因" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="必填，说明为何修改" /></label>
      </div>
      {changedFields.length > 0 && <div className={`amendment-impact ${recalculates ? 'danger' : ''}`}><AlertTriangle size={16} /><span><strong>将修改：</strong>{changedFields.join('、')}。{recalculates ? '股票、方向或发布时间发生变化，旧收益会备份并重新计算。' : '此修改不改变现有收益。'}</span></div>}
      <div className="review-actions"><button className="secondary-button" onClick={() => setEditing(false)}>取消</button><button className="primary-button" disabled={busy || !reason.trim() || !changedFields.length || !/^\d{6}$/.test(form.symbol)} onClick={submit}><Check size={16} />确认修订</button></div>
    </section>}
    {tab === 'overview' && <><EventDossierPanel eventId={event.event_id} />
      <div className="event-fact-grid"><div><span>方向</span><strong>{event.direction === 'long' ? '看多' : '看空'}</strong></div><div><span>事件状态</span><strong>{event.status === 'completed' ? '120日跟踪完成' : isWaiting(event) ? '等待行情基准' : '持续跟踪'}</strong></div><div><span>发布时间</span><strong>{formatDate(event.posted_at)}</strong></div><div><span>来源平台</span><strong>{event.platform}</strong></div></div>
      <section className="event-thesis"><span>推荐理由</span><p>{event.thesis || '原事件没有填写推荐理由'}</p></section>
      {event.execution_warning && event.execution_warning !== 'ok' && <div className="warning-banner event-warning"><AlertTriangle size={16} /><span>{event.execution_warning}</span></div>}
    </>}
    {tab === 'baseline' && <dl className="event-readonly event-baseline"><div><dt>基准规则</dt><dd>{event.baseline_rule || '-'}</dd></div><div><dt>基准日期</dt><dd>{event.baseline_date || '等待行情'}</dd></div><div><dt>真实基准价</dt><dd>{event.baseline_price_raw ? Number(event.baseline_price_raw).toFixed(2) : '待计算'}</dd></div><div><dt>最新行情日期</dt><dd>{event.latest_mark?.trade_date || '待更新'}</dd></div><div><dt>最新观点收益</dt><dd>{event.latest_mark?.directional_return ? `${(Number(event.latest_mark.directional_return) * 100).toFixed(2)}%` : '-'}</dd></div></dl>}
    {tab === 'revisions' && <div className="event-revision-list">{(revisions.data || []).map((revision) => <article key={revision.revision_id}><History size={15} /><div><strong>{revision.changed_fields.join('、')} {revision.recalculation_required && <span className="badge amber">已重算</span>}</strong><p>{revision.reason}</p><small>{formatDate(revision.created_at)}</small></div></article>)}{!revisions.isLoading && !revisions.data?.length && <div className="empty-state compact">尚无人工修订</div>}</div>}
  </>
}

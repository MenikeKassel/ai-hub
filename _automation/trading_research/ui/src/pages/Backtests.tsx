import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, ExternalLink, Rows3, Search, Users,
} from 'lucide-react'
import { api, formatDate, percent } from '../api'
import CandlestickChart, { type PriceChartMarker } from '../components/CandlestickChart'
import EventMethodResearch from '../components/EventMethodResearch'
import type { Event } from '../types'

const CHECKPOINTS = [
  { days: 5, label: '1W' },
  { days: 20, label: '1M' },
  { days: 60, label: '3M' },
  { days: 120, label: '6M' },
]

interface EventGroup {
  symbol: string
  securityName: string
  events: Event[]
}

function warningLabel(value: string): string {
  const labels: Record<string, string> = {
    one_price_limit_suspected: '疑似一字涨停，价格表现不代表可成交收益',
    delayed_baseline: '基准日无有效行情，已顺延到首个可交易日',
    data_conflict: '双源行情存在冲突，检查节点暂未冻结',
    corporate_action_adjustment: '跟踪期发生除权除息，原始价与前复权结果存在差异',
    conditional_intraday_entry_unverified: '推荐包含盘中条件；当前仅统计观点表现，不计入可执行事件统计',
  }
  return value.split(';').filter(Boolean).map((item) => labels[item] || item).join('；')
}

function baselineRuleLabel(value: string): string {
  if (value === 'next_open') return '下一交易日开盘价'
  if (value === 'same_day_close') return '发帖当日收盘价'
  return value || '待确定'
}

function nextCheckpoint(trackingDays: number): { label: string; remaining: number } | null {
  const checkpoint = CHECKPOINTS.find((item) => item.days > trackingDays)
  return checkpoint ? { label: checkpoint.label, remaining: checkpoint.days - trackingDays } : null
}

function eventGroups(events: Event[]): EventGroup[] {
  const groups = new Map<string, EventGroup>()
  for (const event of events) {
    if (!event.symbol) continue
    const group = groups.get(event.symbol) || { symbol: event.symbol, securityName: event.security_name, events: [] }
    group.events.push(event)
    if (!group.securityName && event.security_name) group.securityName = event.security_name
    groups.set(event.symbol, group)
  }
  return [...groups.values()]
    .map((group) => ({ ...group, events: group.events.sort((a, b) => a.posted_at.localeCompare(b.posted_at)) }))
    .sort((a, b) => (b.events.at(-1)?.posted_at || '').localeCompare(a.events.at(-1)?.posted_at || ''))
}

function shiftDate(value: string, days: number): string {
  const [year, month, day] = value.split('-').map(Number)
  const parsed = new Date(Date.UTC(year, month - 1, day))
  parsed.setUTCDate(parsed.getUTCDate() + days)
  return parsed.toISOString().slice(0, 10)
}

function decimal(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '-' : value.toFixed(digits)
}

function contextWarningLabel(value: string): string {
  const labels: Record<string, string> = {
    prior_trade_date_used: '推荐日不是有效交易日或标的无当日行情，使用此前最近完整交易日',
    insufficient_rsi14: 'RSI历史不足',
    insufficient_macd: 'MACD历史不足',
    insufficient_atr14: 'ATR历史不足',
    insufficient_volume_ratio_5: '量比历史不足',
    insufficient_return_20d: '20日收益历史不足',
    insufficient_distance_60d_high: '60日高点历史不足',
    zero_reference_volume: '前5日平均成交量为零',
    expected_trade_date_missing: '目标交易日行情尚未到达，将在行情补齐后自动重算',
    calculation_failed: '技术环境计算失败',
    not_computed: '等待技术环境计算',
  }
  return labels[value] || value
}

export default function Backtests() {
  const events = useQuery({ queryKey: ['events'], queryFn: api.events })
  const summary = useQuery({ queryKey: ['summary'], queryFn: api.summary })
  const checkpoints = useQuery({ queryKey: ['checkpoints'], queryFn: api.checkpoints })
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [search, setSearch] = useState('')
  const activeEvents = useMemo(
    () => events.data?.filter((event) => event.status === 'active' || event.status === 'completed') || [],
    [events.data],
  )
  const groups = useMemo(() => eventGroups(activeEvents), [activeEvents])
  const filteredGroups = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return groups
    return groups.filter((group) => [
      group.symbol,
      group.securityName,
      ...group.events.map((event) => event.kol_name),
    ].some((value) => value.toLowerCase().includes(query)))
  }, [groups, search])
  const selected = filteredGroups.find((group) => group.symbol === selectedSymbol) || filteredGroups[0]
  const firstRecommendationDate = selected?.events[0]?.baseline_date || selected?.events[0]?.posted_at.slice(0, 10) || ''
  const historyStart = firstRecommendationDate ? shiftDate(firstRecommendationDate, -180) : ''
  const stockHistory = useQuery({
    queryKey: ['market-indicators', selected?.symbol, historyStart, 'qfq'],
    queryFn: () => api.marketIndicators(selected!.symbol, historyStart),
    enabled: !!selected && !!historyStart,
  })
  const priceSeries = stockHistory.data?.rows || []
  const priceMarkers = useMemo<PriceChartMarker[]>(() => selected?.events.flatMap((event, index) => [
    {
      date: event.baseline_date || event.posted_at.slice(0, 10),
      label: `#${index + 1} 推荐`,
      kind: 'recommendation' as const,
    },
    ...(event.followups?.map((followup) => ({
      date: followup.posted_at.slice(0, 10),
      label: '复盘',
      kind: 'retrospective' as const,
    })) || []),
  ]) || [], [selected])
  const latestBar = priceSeries.at(-1)
  const uniqueKols = new Set(selected?.events.map((event) => event.kol_name)).size
  const calculatedEvents = selected?.events.filter((event) => event.baseline_date && event.latest_mark).length || 0
  const marketHealth = summary.data?.market

  return <section>
    <header className="page-header">
      <div><span className="eyebrow">PERFORMANCE AUDIT</span><h1>收益审计</h1></div>
      <button className="secondary-button" onClick={() => { window.location.hash = '/events' }}><Rows3 size={16} />管理正式事件</button>
    </header>
    <>
      <div className="context-bar audit-context">
        <span><CheckCircle2 size={15} />这里只展示你已批准的正式推荐事件；股票提及与待审草稿不会进入收益统计。</span>
        <strong>{activeEvents.length} 个事件 · {groups.length} 只股票</strong>
      </div>

      {events.isError && <div className="error-banner">收益事件读取失败：{String(events.error)}</div>}
      {marketHealth?.daily_data_status === 'provider_pending' && <div className="warning-banner audit-warning"><AlertTriangle size={15} /><span>行情供应商尚未发布 {marketHealth.latest_open_date} 日线；当前统一使用最新可用交易日 {marketHealth.latest_daily_date}，涉及 {marketHealth.lagging_symbol_count} 个活跃标的。系统会在下一轮自动补齐。</span></div>}
      {groups.length ? <div className="audit-workbench">
        <aside className="audit-event-list" aria-label="正式跟踪股票">
          <div className="audit-list-heading">
            <div><span>按股票合并</span><strong>{filteredGroups.length} / {groups.length}</strong></div>
            <label className="compact-search"><Search size={14} /><input aria-label="搜索跟踪股票" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="代码、名称或 KOL" /></label>
          </div>
          <div className="audit-event-scroll">{filteredGroups.map((group) => {
            const kols = new Set(group.events.map((event) => event.kol_name)).size
            const awaiting = group.events.filter((event) => !event.baseline_date || !event.latest_mark).length
            return <button key={group.symbol} className={selected?.symbol === group.symbol ? 'audit-event active' : 'audit-event'} onClick={() => setSelectedSymbol(group.symbol)}>
              <div><span className="mono">{group.symbol}</span><span className={awaiting ? 'badge amber' : 'badge green'}>{awaiting ? `${awaiting} 条待行情` : '跟踪中'}</span></div>
              <strong>{group.securityName}</strong>
              <small>{kols} 位 KOL · {group.events.length} 次推荐</small>
              <div className="audit-event-return"><span>最近推荐</span><b>{formatDate(group.events.at(-1)?.posted_at).split(' ')[0]}</b></div>
            </button>
          })}{!filteredGroups.length && <div className="empty-state compact">没有匹配的跟踪股票</div>}</div>
        </aside>

        {selected && <div className="audit-detail">
          <div className="audit-detail-heading">
            <div><div className="audit-detail-meta"><span className="mono">{selected.symbol}</span><span>{uniqueKols} 位 KOL</span><span>{selected.events.length} 次独立推荐</span></div><h2>{selected.securityName}</h2><p>价格时间线按股票合并；每次推荐仍使用自己的发帖时间、基准价和收益口径。</p></div>
          </div>
          <div className="audit-stat-grid">
            <div><span>最新前复权收盘价</span><strong>{latestBar ? Number(latestBar.close).toFixed(/^[15]/.test(selected.symbol) ? 3 : 2) : '-'}</strong><small>{latestBar ? `${latestBar.trade_date} 收盘 · qfq` : '待取数'}</small></div>
            <div><span>独立 KOL</span><strong>{uniqueKols}</strong></div>
            <div><span>已计算事件</span><strong>{calculatedEvents} / {selected.events.length}</strong><small>{selected.events.length - calculatedEvents ? `${selected.events.length - calculatedEvents} 条待行情` : '全部已取数'}</small></div>
            <div><span>历史窗口起点</span><strong>{priceSeries[0]?.trade_date || '待取数'}</strong><small>首次推荐前最多 60 个交易日</small></div>
          </div>

          {stockHistory.isError && <div className="error-banner">历史行情读取失败：{String(stockHistory.error)}</div>}
          <CandlestickChart bars={priceSeries} markers={priceMarkers} securityName={selected.securityName} symbol={selected.symbol} />

          <div className="recommendation-timeline">
            {selected.events.map((event, index) => {
              const trackingDays = Number(event.latest_mark?.tracking_days || 0)
              const upcoming = nextCheckpoint(trackingDays)
              const eventCheckpoints = checkpoints.data?.filter((row) => row.event_id === event.event_id) || []
              const awaiting = !event.baseline_date || !event.latest_mark
              const context = event.technical_context
              return <article className="recommendation-event" key={event.event_id}>
                <div className="timeline-marker">#{index + 1}</div>
                <div className="recommendation-main">
                  <div className="recommendation-heading"><div><strong>{event.kol_name}</strong><span>{formatDate(event.posted_at)}</span></div><a className="icon-button" href={event.source_url} target="_blank" rel="noreferrer" title="打开原帖"><ExternalLink size={15} /></a></div>
                  <p>{event.thesis}</p>
                  {awaiting && <div className="warning-banner audit-warning"><CircleDashed size={15} />等待可用基准行情；不会使用发帖前价格补位</div>}
                  {event.execution_warning && event.execution_warning !== 'ok' && <div className="warning-banner audit-warning"><AlertTriangle size={15} />{warningLabel(event.execution_warning)}</div>}
                  {event.followups?.map((followup) => <div className="recommendation-followup" key={followup.post_id}>
                    <div><span className="badge neutral">复盘证据</span><span>{formatDate(followup.posted_at)}</span><a className="icon-button" href={followup.url} target="_blank" rel="noreferrer" title="打开复盘帖"><ExternalLink size={14} /></a></div>
                    <p>{followup.text || '复盘帖子'}</p>
                  </div>)}
                  <details className="technical-context">
                    <summary>
                      <span><Activity size={14} />推荐时市场状态</span>
                      <small>{context?.as_of_trade_date ? `${context.as_of_trade_date} · 前复权` : '等待计算'}</small>
                      <ChevronDown size={14} />
                    </summary>
                    {context ? <>
                      <div className="technical-context-grid">
                        <div><span>RSI14</span><strong>{decimal(context.rsi14, 1)}</strong></div>
                        <div><span>MACD柱 / 收盘</span><strong>{percent(context.macd_hist_pct)}</strong></div>
                        <div><span>ATR14 / 收盘</span><strong>{percent(context.atr14_pct)}</strong></div>
                        <div><span>5日量比</span><strong>{context.volume_ratio_5 === null ? '-' : `${decimal(context.volume_ratio_5)}x`}</strong></div>
                        <div><span>20日收益</span><strong>{percent(context.return_20d)}</strong></div>
                        <div><span>距60日高点</span><strong>{percent(context.distance_60d_high)}</strong></div>
                      </div>
                      <div className="technical-context-foot">
                        <span>{context.status === 'complete' ? '数据完整' : context.status === 'partial' ? '部分历史不足' : '等待计算'}</span>
                        <span>{context.feature_version}</span>
                      </div>
                      {!!context.warnings.length && <p className="technical-context-warning">{context.warnings.map(contextWarningLabel).join('；')}</p>}
                      {context.error && <p className="technical-context-error">{context.error}</p>}
                    </> : <p className="technical-context-empty">行情补齐后自动生成；该状态不参与KOL排名或买卖判断。</p>}
                  </details>
                  <EventMethodResearch eventId={event.event_id} />
                  <details className="technical-context intraday-context">
                    <summary><span><Activity size={14} />Intraday event window</span><small>{event.intraday_context?.status || 'pending'}</small><ChevronDown size={14} /></summary>
                    {event.intraday_context ? <>
                      <div className="technical-context-grid">
                        <div><span>First price</span><strong>{event.intraday_context.first_price == null ? '-' : Number(event.intraday_context.first_price).toFixed(2)}</strong></div>
                        <div><span>5m close</span><strong>{event.intraday_context.close_5m == null ? '-' : Number(event.intraday_context.close_5m).toFixed(2)}</strong></div>
                        <div><span>15m close</span><strong>{event.intraday_context.close_15m == null ? '-' : Number(event.intraday_context.close_15m).toFixed(2)}</strong></div>
                        <div><span>30m close</span><strong>{event.intraday_context.close_30m == null ? '-' : Number(event.intraday_context.close_30m).toFixed(2)}</strong></div>
                        <div><span>60m close</span><strong>{event.intraday_context.close_60m == null ? '-' : Number(event.intraday_context.close_60m).toFixed(2)}</strong></div>
                        <div><span>Window close</span><strong>{event.intraday_context.close_window == null ? '-' : Number(event.intraday_context.close_window).toFixed(2)}</strong></div>
                        <div><span>MFE</span><strong>{percent(event.intraday_context.mfe)}</strong></div>
                        <div><span>MAE</span><strong>{percent(event.intraday_context.mae)}</strong></div>
                      </div>
                      {!!event.intraday_context.warnings?.length && <p className="technical-context-warning">{event.intraday_context.warnings.join('; ')}</p>}
                    </> : <p className="technical-context-empty">No event-window minute context yet. It is filled from the isolated FreeStockDB mirror after daily sync.</p>}
                  </details>
                </div>
                <div className="recommendation-metrics">
                  <div><span>基准</span><strong>{event.baseline_date || '待取数'} / {event.baseline_price_raw ? Number(event.baseline_price_raw).toFixed(2) : '-'}</strong><small>{baselineRuleLabel(event.baseline_rule)}</small></div>
                  <div><span>当前收益</span><strong>{percent(event.latest_mark?.directional_return)}</strong><small>{event.latest_mark?.trade_date || '待取数'}</small></div>
                  <div><span>同期超额</span><strong>{percent(event.latest_mark?.directional_excess_return)}</strong><small>相对沪深300</small></div>
                  <div><span>最大有利 MFE</span><strong>{percent(event.latest_mark?.max_favorable_return)}</strong><small>{trackingDays} 个交易日</small></div>
                  <div><span>最大不利 MAE</span><strong>{percent(event.latest_mark?.max_adverse_return)}</strong><small>{trackingDays} 个交易日</small></div>
                  <div><span>检查点</span><strong>{eventCheckpoints.length} / 4</strong><small>{upcoming ? `${upcoming.label} 还差 ${upcoming.remaining} 日` : '120日完成'}</small></div>
                </div>
              </article>
            })}
          </div>
        </div>}
      </div> : <div className="panel empty-state">当前没有正式跟踪事件</div>}
      <div className="audit-boundary"><Users size={15} /><span>同一股票按价格时间线合并展示；KOL事件、基准价、收益和检查节点始终独立计算。</span></div>
    </>
  </section>
}

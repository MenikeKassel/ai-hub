import { useMemo, useState } from 'react'
import { BarChart3, RefreshCw, ShieldCheck, TrendingUp } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, percent } from '../api'
import type { KolPerformanceRow } from '../types'

const horizons = ['1W', '1M', '3M', '6M'] as const
const windows = ['all', '7', '30', '90'] as const

function tierClass(tier: KolPerformanceRow['tier']): string {
  return tier === 'long_term' || tier === 'reliable' ? 'green' : tier === 'provisional' || tier === 'watch' ? 'amber' : 'neutral'
}

function trendMetrics(payload: KolPerformanceRow | KolPerformanceRow['metrics']): KolPerformanceRow['metrics'] {
  // Historical snapshots created before v4 stored the metrics object directly
  // in payload, while current snapshots wrap it in a full performance row.
  // Normalize both shapes so the read-only console never crashes on legacy
  // restored snapshots.
  return ('metrics' in payload && payload.metrics ? payload.metrics : payload) as KolPerformanceRow['metrics']
}

function Trend({ points }: { points: Array<{ as_of: string; payload: KolPerformanceRow | KolPerformanceRow['metrics'] }> }) {
  const values = points.map((point) => trendMetrics(point.payload).median_excess).filter((value): value is number => value !== null && Number.isFinite(value))
  if (values.length < 2) return <div className="empty-state compact">运行更多检查节点后显示滚动趋势。</div>
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const coords = points.map((point, index) => {
    const value = trendMetrics(point.payload).median_excess
    if (value === null) return null
    const x = 18 + (index / Math.max(points.length - 1, 1)) * 564
    const y = 150 - ((value - min) / span) * 120
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).filter(Boolean).join(' ')
  return <div className="kol-performance-trend">
    <svg viewBox="0 0 600 180" role="img" aria-label="KOL中位超额收益滚动趋势">
      <line x1="18" x2="582" y1="150" y2="150" stroke="#d8dde4" />
      <line x1="18" x2="582" y1="30" y2="30" stroke="#d8dde4" strokeDasharray="3 4" />
      <polyline fill="none" stroke="#176b55" strokeWidth="3" points={coords} />
      {points.map((point, index) => {
        const value = trendMetrics(point.payload).median_excess
        if (value === null) return null
        const x = 18 + (index / Math.max(points.length - 1, 1)) * 564
        const y = 150 - ((value - min) / span) * 120
        return <circle key={`${point.as_of}-${index}`} cx={x} cy={y} r="3" fill="#176b55"><title>{`${point.as_of} ${percent(value)}`}</title></circle>
      })}
      <text x="18" y="172" fontSize="9" fill="#697386">{points[0]?.as_of || ''}</text>
      <text x="582" y="172" textAnchor="end" fontSize="9" fill="#697386">{points[points.length - 1]?.as_of || ''}</text>
      <text x="582" y="28" textAnchor="end" fontSize="9" fill="#697386">{percent(max)}</text>
      <text x="582" y="148" textAnchor="end" fontSize="9" fill="#697386">{percent(min)}</text>
    </svg>
  </div>
}

export default function KolPerformance() {
  const client = useQueryClient()
  const [platform, setPlatform] = useState('all')
  const [windowName, setWindowName] = useState<(typeof windows)[number]>('all')
  const [horizon, setHorizon] = useState<(typeof horizons)[number]>('1W')
  const [selectedKey, setSelectedKey] = useState<string>('')
  const query = useQuery({
    queryKey: ['kol-performance', platform, windowName, horizon],
    queryFn: () => api.kolPerformance({ platform, window: windowName, horizon }),
  })
  const selected = selectedKey || query.data?.rows[0]?.kol_key || ''
  const detail = useQuery({ queryKey: ['kol-performance-detail', selected], queryFn: () => api.kolPerformanceDetail(selected), enabled: Boolean(selected) })
  const refresh = useMutation({ mutationFn: () => api.refreshKolPerformance(), onSuccess: () => { client.invalidateQueries({ queryKey: ['kol-performance'] }); client.invalidateQueries({ queryKey: ['kol-performance-detail'] }) } })
  const rows = query.data?.rows || []
  const selectedRow = useMemo(() => rows.find((row) => row.kol_key === selected) || detail.data?.row, [detail.data?.row, rows, selected])
  const metrics = selectedRow?.metrics

  return <section className="kol-performance-page">
    <header className="page-header"><div><span className="eyebrow">CAPABILITY & STABILITY</span><h1>KOL表现</h1><p className="page-subtitle">按看多推荐帖子批次等权统计；看空事件保留审计记录，不计入A股收益或排名，AI解读不参与排名。</p></div><button className="primary-button" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw size={16} className={refresh.isPending ? 'spin' : ''} />刷新快照</button></header>
    <div className="kol-performance-filters segmented" aria-label="KOL表现筛选"><label>平台<select value={platform} onChange={(event) => setPlatform(event.target.value)}><option value="all">全部平台</option><option value="X">X</option><option value="Zhihu">知乎</option></select></label><label>结果窗口<select value={windowName} onChange={(event) => setWindowName(event.target.value as (typeof windows)[number])}>{windows.map((value) => <option key={value} value={value}>{value === 'all' ? '全部' : `${value}日`}</option>)}</select></label><label>成熟节点<select value={horizon} onChange={(event) => setHorizon(event.target.value as (typeof horizons)[number])}>{horizons.map((value) => <option key={value} value={value}>{value}</option>)}</select></label></div>
    <div className="kol-performance-kpis"><div><small>数据截至</small><strong>{query.data?.as_of || '-'}</strong></div><div><small>主分析KOL</small><strong>{query.data?.coverage.kol_count ?? '-'}</strong></div><div><small>可排名人数</small><strong>{query.data?.coverage.ranked_count ?? '-'}</strong></div><div><small>成熟批次</small><strong>{query.data?.coverage.mature_batch_count ?? '-'}</strong></div><div><small>未成熟批次</small><strong>{query.data?.coverage.unmatured_batch_count ?? '-'}</strong></div></div>
    {query.error && <div className="error-banner">{String(query.error)}</div>}
    <div className="kol-performance-layout"><div className="panel kol-performance-list"><div className="panel-heading"><div><h2>平台内观察表</h2><span>样本中不显示正式名次</span></div><ShieldCheck size={17} /></div><div className="table-scroll"><table><thead><tr><th>名次</th><th>KOL</th><th>阶段</th><th>批次/股票</th><th>中位超额</th><th>胜率</th><th>MAE</th></tr></thead><tbody>{rows.map((row) => { const value = row.metrics; return <tr key={row.kol_key} className={row.kol_key === selected ? 'selected' : ''} onClick={() => setSelectedKey(row.kol_key)}><td className="mono">{row.rank || '-'}</td><td><strong>{row.kol_name}</strong><span className="secondary-line">{row.platform} · {row.kol_handle || 'legacy'}</span></td><td><span className={`badge ${tierClass(row.tier)}`}>{row.tier_label}</span></td><td>{value.batch_count} / {value.unique_symbols}<span className="secondary-line">成熟 {value.recommendation_days} 个推荐日</span></td><td><strong>{percent(value.median_excess)}</strong><span className="secondary-line">均值 {percent(value.mean_excess)}</span></td><td>{percent(value.win_rate)}</td><td>{percent(value.median_mae)}</td></tr> })}</tbody></table></div>{!query.isLoading && !rows.length && <div className="empty-state">暂无表现快照</div>}</div><div className="panel kol-performance-detail"><div className="panel-heading"><div><h2>{selectedRow?.kol_name || '选择一个KOL'}</h2><span>{selectedRow?.platform || ''} · {selectedRow?.tier_label || ''} · {horizon}</span></div><TrendingUp size={18} /></div>{selectedRow && metrics ? <><div className="performance-fact-grid"><div><small>批次</small><strong>{metrics.batch_count}</strong></div><div><small>看多事件</small><strong>{metrics.long_event_count}</strong></div><div><small>推荐日</small><strong>{metrics.recommendation_days}</strong></div><div><small>唯一股票</small><strong>{metrics.unique_symbols}</strong></div><div><small>中位超额</small><strong>{percent(metrics.median_excess)}</strong></div><div><small>批次胜率</small><strong>{percent(metrics.win_rate)}</strong></div><div><small>中位MAE</small><strong>{percent(metrics.median_mae)}</strong></div><div><small>中位MFE</small><strong>{percent(metrics.median_mfe)}</strong></div></div><div className="section-heading"><h3>滚动中位超额</h3><span>仅使用已落地检查节点</span></div><Trend points={detail.data?.series?.[horizon] || []} /><div className="performance-limitations"><strong>统计限制</strong><span>{metrics.confidence_interval ? `Bootstrap中位数95%区间：${percent(metrics.confidence_interval.lower)} 至 ${percent(metrics.confidence_interval.upper)}` : '样本少于5个批次，不生成置信区间。'}</span><span>未成熟批次：{metrics.unmatured_batch_count}；看空审计事件 {metrics.short_event_count} 条不计入批次、胜率或超额收益；同帖多股已等权，不模拟真实仓位、手续费或资金占用。</span></div></> : <div className="empty-state">等待选择KOL</div>}</div></div>
    {selectedRow?.narrative && <div className="panel kol-performance-narrative"><div className="panel-heading"><div><h2>阶段解读</h2><span>{selectedRow.narrative.provider === 'rules' ? '确定性模板；可由 OpenCode Go AI 替换，不参与统计' : `${selectedRow.narrative.provider} / ${selectedRow.narrative.model}`}</span></div><BarChart3 size={17} /></div><div className="narrative-body"><p>{selectedRow.narrative.summary}</p><div className="narrative-columns"><div><strong>优势</strong>{selectedRow.narrative.strengths.map((item) => <span key={item}>{item}</span>)}</div><div><strong>风险</strong>{selectedRow.narrative.risks.map((item) => <span key={item}>{item}</span>)}</div><div><strong>限制</strong>{selectedRow.narrative.limitations.map((item) => <span key={item}>{item}</span>)}</div></div></div></div>}
    <div className="scope-note"><BarChart3 size={15} />主表排除看空事件、条件交易、一字板、来源冲突和二手事件；这些记录仍保留在收益审计或次级观察区。</div>
  </section>
}

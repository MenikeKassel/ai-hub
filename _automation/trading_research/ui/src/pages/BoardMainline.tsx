import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  BarChart3,
  ExternalLink,
  RefreshCw,
  Search,
} from 'lucide-react'
import { api, formatDate, percent } from '../api'
import BoardRpsChart from '../components/BoardRpsChart'
import BoardRankTrendChart from '../components/BoardRankTrendChart'
import CandlestickChart from '../components/CandlestickChart'
import type {
  BoardMainlineItem,
  BoardMainlineStatus,
  BoardType,
  MarketDailyBar,
} from '../types'

const statusLabels: Record<BoardMainlineStatus, string> = {
  persistent_candidate: '持续候选',
  mainline_candidate: '主线候选',
  strong_watch: '强势观察',
  rps_only: '仅 RPS',
  partial_universe: '覆盖不足',
  neutral: '中性',
}

const warningLabels: Record<string, string> = {
  current_universe_backfill_bias: '历史使用当前板块列表，存在成分变动与幸存者偏差',
  partial_universe: '有效板块覆盖不足 90%，不发布主线标签',
  breadth_unavailable: '缺少上涨宽度，只展示 RPS',
  turnover_history_insufficient: '量能历史不足，只展示 RPS',
}

function score(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? '-'
    : Number(value).toFixed(1)
}

function statusTone(value: BoardMainlineStatus): string {
  if (value === 'persistent_candidate' || value === 'mainline_candidate') return 'green'
  if (value === 'strong_watch') return 'amber'
  if (value === 'partial_universe') return 'red'
  return value === 'rps_only' ? 'blue' : 'neutral'
}

export default function BoardMainline() {
  const client = useQueryClient()
  const [boardType, setBoardType] = useState<BoardType>('industry')
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [selectedCode, setSelectedCode] = useState('')
  const [rankWindow, setRankWindow] = useState<50 | 120 | 250>(50)
  const [rankRange, setRankRange] = useState<'120' | '250' | 'all'>('120')
  const params = useMemo(() => {
    const value = new URLSearchParams({
      board_type: boardType,
      page: '1',
      page_size: '200',
      sort_by: 'rps_50',
      descending: 'true',
    })
    if (status) value.set('status', status)
    if (search.trim()) value.set('q', search.trim())
    return value
  }, [boardType, search, status])

  const health = useQuery({
    queryKey: ['board-health'],
    queryFn: api.boardHealth,
    refetchInterval: 20_000,
  })
  const listing = useQuery({
    queryKey: ['board-mainline', params.toString()],
    queryFn: () => api.boardMainline(params),
  })
  useEffect(() => {
    const items = listing.data?.items || []
    if (!items.length) {
      setSelectedCode('')
      return
    }
    if (!items.some((item) => item.board_code === selectedCode)) {
      setSelectedCode(items[0].board_code)
    }
  }, [listing.data?.items, selectedCode])
  const detail = useQuery({
    queryKey: ['board-detail', boardType, selectedCode],
    queryFn: () => api.boardDetail(selectedCode, boardType),
    enabled: Boolean(selectedCode),
  })
  const series = useQuery({
    queryKey: ['board-series', boardType, selectedCode],
    queryFn: () => api.boardSeries(selectedCode, boardType),
    enabled: Boolean(selectedCode),
  })
  const rankSeries = useQuery({
    queryKey: ['board-rank-series', boardType, selectedCode, rankWindow, rankRange],
    queryFn: () => api.boardRankSeries(selectedCode, boardType, rankWindow, rankRange),
    enabled: Boolean(selectedCode),
  })
  const sync = useMutation({
    mutationFn: api.syncBoards,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['board-health'] })
      client.invalidateQueries({ queryKey: ['board-mainline'] })
    },
  })
  const selected = listing.data?.items.find((item) => item.board_code === selectedCode)
    || detail.data?.latest
  const bars: MarketDailyBar[] = (series.data || []).flatMap((row) => (
    row.open && row.high && row.low && row.close
      ? [{
        symbol: selectedCode,
        trade_date: row.trade_date,
        open: Number(row.open),
        high: Number(row.high),
        low: Number(row.low),
        close: Number(row.close),
        volume: row.volume || undefined,
        amount: row.amount || undefined,
        adjustment: 'raw',
        provider: 'akshare-eastmoney',
      }]
      : []
  ))
  const sourceBlocked = health.data?.status === 'source_blocked' || health.data?.status === 'failed'
  const partialCoverage = health.data?.status === 'partial_coverage'
  const error = health.error || listing.error || detail.error || series.error || rankSeries.error || sync.error

  return <section>
    <header className="page-header">
      <div>
        <span className="eyebrow">SECTOR MAINLINE</span>
        <h1>板块主线</h1>
        <p className="page-subtitle">RPS50 / 120 / 250 与板块宽度、量能共同确认，只作为相对强度研究状态。</p>
      </div>
      <div className="header-actions">
        <div className="segmented" aria-label="板块分类">
          <button className={boardType === 'industry' ? 'active' : ''} onClick={() => setBoardType('industry')}>行业</button>
          <button className={boardType === 'concept' ? 'active' : ''} onClick={() => setBoardType('concept')}>概念</button>
        </div>
        <button className="secondary-button" disabled={sync.isPending} onClick={() => sync.mutate()}>
          <RefreshCw size={16} className={sync.isPending ? 'spin' : ''} />立即同步
        </button>
      </div>
    </header>

    {error && <div className="error-banner">{String(error)}</div>}
    {sourceBlocked && <div className="warning-banner board-source-warning">
      <AlertTriangle size={16} />
      <span>东方财富板块源当前受限。系统保留最后有效数据，不会用空结果重算或发布主线标签。</span>
    </div>}
    {partialCoverage && <div className="warning-banner board-source-warning">
      <AlertTriangle size={16} />
      <span>
        最新交易日仅覆盖 {percent(health.data?.coverage_ratios?.[boardType])} 的
        {boardType === 'industry' ? '行业' : '概念'}板块；缺失板块不补零，当日不发布主线标签。
      </span>
    </div>}

    <div className="board-health-strip">
      <HealthFact label="运行状态" value={health.data?.status || 'loading'} />
      <HealthFact label="板块目录" value={health.data?.catalog_counts?.[boardType] ?? 0} />
      <HealthFact label="已计算 RPS" value={health.data?.rps_counts?.[boardType] ?? 0} />
      <HealthFact label="最新日覆盖" value={percent(health.data?.coverage_ratios?.[boardType])} />
      <HealthFact label="最新交易日" value={health.data?.latest_trade_dates?.[boardType] || '-'} />
      <HealthFact label="待回填" value={health.data?.pending_backfill ?? 0} />
    </div>

    <div className="board-toolbar">
      <label className="search-field">
        <Search size={15} />
        <input aria-label="搜索板块" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索板块名称或代码" />
      </label>
      <select aria-label="筛选主线状态" value={status} onChange={(event) => setStatus(event.target.value)}>
        <option value="">全部状态</option>
        {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      <span>{listing.data?.total || 0} 个{boardType === 'industry' ? '行业' : '概念'}板块</span>
    </div>

    <div className="board-workbench">
      <div className="panel board-list-panel">
        <div className="table-scroll board-table-scroll">
          <table>
            <thead><tr><th>板块</th><th>状态</th><th>RPS50</th><th>RPS120</th><th>RPS250</th><th>宽度</th><th>量能</th></tr></thead>
            <tbody>{listing.data?.items.map((item) => <BoardRow
              key={item.board_key}
              item={item}
              active={item.board_code === selectedCode}
              onSelect={() => setSelectedCode(item.board_code)}
            />)}</tbody>
          </table>
          {!listing.isLoading && !listing.data?.items.length && <div className="empty-state">
            {health.data?.status === 'empty' ? '等待板块目录和历史行情首次回填' : '当前筛选条件没有板块'}
          </div>}
        </div>
      </div>

      <div className="panel board-detail-panel">
        {selected && detail.data ? <>
          <div className="board-detail-heading">
            <div>
              <span className="eyebrow">{detail.data.board_code} · {detail.data.board_type === 'industry' ? '行业' : '概念'}</span>
              <h2>{detail.data.board_name}</h2>
            </div>
            <span className={`badge ${statusTone(selected.status)}`}>{statusLabels[selected.status]}</span>
          </div>
          <div className="board-fact-grid">
            <HealthFact label="最新指数" value={score(selected.close)} />
            <HealthFact label="RPS50" value={score(selected.rps_50)} />
            <HealthFact label="上涨宽度" value={percent(selected.breadth)} />
            <HealthFact label="20日量能比" value={score(selected.turnover_ratio_20)} />
          </div>
          {!!selected.warnings?.length && <div className="board-warning-list">
            {selected.warnings.map((item) => <span key={item}><AlertTriangle size={13} />{warningLabels[item] || item}</span>)}
          </div>}
          <CandlestickChart bars={bars} markers={[]} securityName={detail.data.board_name} symbol={selectedCode} />
          <div className="board-rank-toolbar">
            <div className="segmented" role="tablist" aria-label="排名周期">
              {[50, 120, 250].map((window) => <button
                key={window}
                className={rankWindow === window ? 'active' : ''}
                onClick={() => setRankWindow(window as 50 | 120 | 250)}
              >RPS{window}</button>)}
            </div>
            <div className="segmented" role="tablist" aria-label="排名日期范围">
              {([
                ['120', '120日'],
                ['250', '250日'],
                ['all', '全部'],
              ] as const).map(([value, label]) => <button
                key={value}
                className={rankRange === value ? 'active' : ''}
                onClick={() => setRankRange(value)}
              >{label}</button>)}
            </div>
            {rankSeries.data?.truncated && <span className="badge amber">显示最近 {rankRange} 个有效交易日</span>}
          </div>
          <BoardRankTrendChart series={rankSeries.data} rangeName={rankRange} />
          <details className="board-score-details">
            <summary>查看RPS分位值曲线</summary>
            <BoardRpsChart rows={series.data || []} />
          </details>

          <div className="board-related-grid">
            <div>
              <div className="section-heading"><h3>领涨与成分</h3><span>{detail.data.members.length} 个成分快照</span></div>
              <div className="board-member-list">
                {selected.leader_name && <strong>领涨：{selected.leader_name}</strong>}
                {detail.data.members.slice(0, 18).map((item) => <span key={item.symbol}><b>{item.symbol}</b>{item.security_name}</span>)}
                {!detail.data.members.length && <small>主线候选确定后，周日任务补充成分股快照。</small>}
              </div>
            </div>
            <div>
              <div className="section-heading"><h3>关联 KOL 事件</h3><span>{detail.data.related_events.length} 条</span></div>
              <div className="board-event-list">
                {detail.data.related_events.map((event) => <a key={event.event_id} href={event.source_url} target="_blank" rel="noreferrer">
                  <div><strong>{event.security_name}</strong><span>{event.symbol}</span></div>
                  <p>{event.kol_name} · {formatDate(event.posted_at)}</p>
                  <ExternalLink size={13} />
                </a>)}
                {!detail.data.related_events.length && <small>暂无正式推荐事件映射到该板块。</small>}
              </div>
            </div>
          </div>
        </> : <div className="empty-state board-detail-empty"><BarChart3 size={24} />选择一个板块查看 K 线、RPS 与关联事件</div>}
      </div>
    </div>
  </section>
}

function BoardRow({ item, active, onSelect }: { item: BoardMainlineItem; active: boolean; onSelect: () => void }) {
  return <tr className={active ? 'board-row active' : 'board-row'} onClick={onSelect}>
    <td><button className="board-name-button" onClick={onSelect}><strong>{item.board_name}</strong><span>{item.board_code} · {item.trade_date}</span></button></td>
    <td><span className={`badge ${statusTone(item.status)}`}>{statusLabels[item.status]}</span></td>
    <td className="return-value">{score(item.rps_50)}</td>
    <td className="return-value">{score(item.rps_120)}</td>
    <td className="return-value">{score(item.rps_250)}</td>
    <td>{percent(item.breadth)}</td>
    <td>{score(item.turnover_ratio_20)}</td>
  </tr>
}

function HealthFact({ label, value }: { label: string; value: string | number }) {
  return <div><span>{label}</span><strong>{value}</strong></div>
}

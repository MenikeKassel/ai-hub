import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Archive, Database, Pin, Play, RefreshCw } from 'lucide-react'
import { api, formatDate } from '../api'
import type { MarketCoverage } from '../types'

export default function MarketData() {
  const client = useQueryClient()
  const instruments = useQuery({ queryKey: ['instruments'], queryFn: api.instruments })
  const health = useQuery({ queryKey: ['market'], queryFn: api.marketHealth, refetchInterval: 30_000 })
  const sync = useMutation({ mutationFn: () => api.enqueueMarketSync(), onSuccess: () => client.invalidateQueries({ queryKey: ['market'] }) })
  const updateFreeStockDB = useMutation({ mutationFn: () => api.updateFreeStockDB(), onSuccess: () => client.invalidateQueries({ queryKey: ['market'] }) })
  const patch = useMutation({ mutationFn: ({ symbol, lifecycle }: { symbol: string; lifecycle: 'pinned' | 'tracking' | 'archived' }) => api.patchInstrument(symbol, { lifecycle }), onSuccess: () => { client.invalidateQueries({ queryKey: ['instruments'] }); client.invalidateQueries({ queryKey: ['market'] }) } })
  const coverage = useMemo(() => {
    const map = new Map<string, MarketCoverage[]>()
    for (const row of health.data?.coverage || []) map.set(row.symbol, [...(map.get(row.symbol) || []), row])
    return map
  }, [health.data?.coverage])
  const value = health.data
  const freeStock = value?.freestockdb
  const sampleLatest = Object.values(freeStock?.provider?.samples || {})
    .map((item) => item.latest || '')
    .filter(Boolean)
    .sort()
    .at(-1)

  return <section>
    <header className="page-header">
      <div><span className="eyebrow">MARKET DATA</span><h1>数据中心</h1></div>
      <button className="primary-button" disabled={sync.isPending} onClick={() => sync.mutate()}><Play size={16} />加入同步队列</button>
    </header>
    {(health.error || instruments.error || sync.error || patch.error) && <div className="error-banner">{String(health.error || instruments.error || sync.error || patch.error)}</div>}
    <div className="panel provider-panel">
      <div className="panel-heading">
        <div><h2>FreeStockDB fallback provider</h2><span>Local mirror for freshness gaps and intraday event windows. BaoStock remains the formal primary source.</span></div>
        <div className="actions">
          <button className="secondary-button" onClick={() => client.invalidateQueries({ queryKey: ['market'] })}><RefreshCw size={15} />Check</button>
          <button className="secondary-button" disabled={updateFreeStockDB.isPending} onClick={() => updateFreeStockDB.mutate()}><Play size={15} />Update mirror</button>
        </div>
      </div>
      <div className="provider-status-grid">
        <span>Service <strong>{freeStock?.service_status || 'unknown'}</strong></span>
        <span>Freshness <strong>{freeStock?.freshness?.status || 'unknown'}</strong></span>
        <span>Latest sample <strong>{sampleLatest || '-'}</strong></span>
        <span>Expected <strong>{freeStock?.freshness?.expected_trade_date || '-'}</strong></span>
        <span>Update <strong>{freeStock?.last_update?.phase || freeStock?.last_update?.status || 'idle'}</strong></span>
        <span>Layout <strong>{freeStock?.storage_layout?.status || 'unknown'}</strong></span>
        <span>Next staging <strong>{freeStock?.disk?.staging_strategy || 'unknown'}</strong></span>
        <span>Catalog <strong>{freeStock?.catalog_symbols ?? '-'} symbols</strong></span>
        <span>Manifest <strong>{freeStock?.manifest?.file_count ?? '-'} files</strong></span>
        <span>Disk <strong>{freeStock?.disk?.free_gb ?? '-'} GB free</strong></span>
      </div>
      {freeStock?.freshness?.status === 'stale' && <div className="error-banner">Mirror data is stale. Expected {freeStock.freshness.expected_trade_date || 'the latest trading day'}, latest sample {sampleLatest || 'missing'}. The service being online does not mean its data is current.</div>}
      {freeStock?.last_update?.status === 'running' && <div className="warning-banner">Mirror update is running: {freeStock.last_update.phase || 'working'}. A/B updates keep the verified dataset online until the final swap; the first bootstrap may pause briefly while taking its snapshot.</div>}
      {freeStock?.last_update?.status === 'failed' && <div className="error-banner">Last mirror update failed: {freeStock.last_update.error || 'unknown error'}</div>}
      {freeStock?.storage_migrated === false && <div className="error-banner">Storage layout is {freeStock.storage_layout?.status || 'invalid'}. Updates are blocked until the compatibility link points from the program directory to the D-drive live dataset.</div>}
      {freeStock?.update_ready === false && <div className="warning-banner">Safe mirror update is paused: {freeStock.disk?.required_for_safe_update_gb ?? '-'} GB free is required for the next staging generation.</div>}
      {freeStock?.transport_warning && <div className="warning-banner">Transport warning: this mirror is HTTP and remains an untrusted secondary source. It cannot freeze formal returns alone.</div>}
      {updateFreeStockDB.error && <div className="error-banner">{String(updateFreeStockDB.error)}</div>}
    </div>
    <div className="metric-grid">
      <Metric label="证券主数据" value={value?.instrument_count ?? '-'} />
      <Metric label="活跃标的" value={value?.active_instruments ?? '-'} />
      <Metric label="数据覆盖" value={value?.coverage_count ?? '-'} />
      <Metric label="质量警告" value={value?.warning_count ?? '-'} warning={!!value?.warning_count} />
    </div>
    <div className="content-grid market-grid">
      <div className="panel">
        <div className="panel-heading"><div><h2>标的数据覆盖</h2><span>不复权与前复权分开保存</span></div><RefreshCw size={16} /></div>
        <div className="table-scroll"><table><thead><tr><th>标的</th><th>类型</th><th>生命周期</th><th>覆盖范围</th><th>来源</th><th>质量</th><th aria-label="操作" /></tr></thead>
          <tbody>{instruments.data?.map((item) => {
            const rows = coverage.get(item.symbol) || []
            const daily = rows.filter((row) => row.dataset === 'daily')
            const start = daily.map((row) => row.start_date).sort()[0]
            const end = daily.map((row) => row.end_date).sort().at(-1)
            const warning = daily.some((row) => row.quality_status !== 'valid')
            return <tr key={item.symbol}>
              <td><strong className="mono">{item.symbol}</strong><span className="secondary-line">{item.name}</span></td>
              <td>{item.instrument_type}</td>
              <td><span className={`badge ${item.lifecycle === 'pinned' ? 'green' : item.lifecycle === 'tracking' ? 'blue' : 'neutral'}`}>{item.lifecycle}</span></td>
              <td>{start && end ? <><strong>{start}</strong><span className="secondary-line">至 {end} · {daily.length}组</span></> : '待同步'}</td>
              <td>{daily.map((row) => row.provider).filter((v, i, a) => a.indexOf(v) === i).join(' / ') || item.source}</td>
              <td><span className={`badge ${warning ? 'amber' : daily.length ? 'green' : 'neutral'}`}>{warning ? 'warning' : daily.length ? 'valid' : 'missing'}</span></td>
              <td className="actions"><button className="icon-button" title="置顶持续更新" onClick={() => patch.mutate({ symbol: item.symbol, lifecycle: 'pinned' })}><Pin size={15} /></button><button className="icon-button" title="停止每日更新" onClick={() => patch.mutate({ symbol: item.symbol, lifecycle: 'archived' })}><Archive size={15} /></button></td>
            </tr>
          })}</tbody></table></div>
          {!instruments.isLoading && !instruments.data?.length && <div className="empty-state">尚未初始化证券主数据</div>}
      </div>
      <div className="panel issue-panel">
        <div className="panel-heading"><div><h2>近期质量问题</h2><span>错误批次不会覆盖合格数据</span></div></div>
        <div className="issue-list">{value?.issues?.slice(0, 12).map((issue, index) => <div key={`${issue.run_id}-${issue.code}-${index}`}>
          <AlertTriangle size={15} className={issue.severity === 'error' ? 'issue-error' : 'issue-warning'} />
          <div><strong>{issue.symbol} · {issue.code}</strong><p>{issue.message}</p><small>{formatDate(issue.created_at)}</small></div>
        </div>)}{!value?.issues?.length && <div className="empty-state">暂无数据质量问题</div>}</div>
        <div className="panel-heading"><div><h2>同步队列</h2><span>{value?.queue?.length || 0} 项待处理</span></div></div>
        <div className="queue-list">{value?.queue?.slice(0, 10).map((item) => <div key={item.queue_key}><Database size={14} /><strong>{item.symbol}</strong><span>{item.reason}</span></div>)}{!value?.queue?.length && <div className="empty-state compact">队列为空</div>}</div>
      </div>
    </div>
  </section>
}

function Metric({ label, value, warning = false }: { label: string; value: string | number; warning?: boolean }) {
  return <div className="metric"><div className={`metric-icon ${warning ? 'amber' : 'neutral'}`}><Database size={18} /></div><div><span>{label}</span><strong>{value}</strong></div></div>
}

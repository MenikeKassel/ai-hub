import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, CheckCircle2, Clock3, Database, Radio, Search } from 'lucide-react'
import { api, formatDate, percent } from '../api'

export default function Overview({ onReview }: { onReview: () => void }) {
  const summary = useQuery({ queryKey: ['summary'], queryFn: api.summary, refetchInterval: 30_000 })
  const events = useQuery({ queryKey: ['events'], queryFn: api.events })
  const value = summary.data
  const active = (events.data || []).filter((event) => event.status === 'active').slice(0, 6)

  return (
    <section>
      <header className="page-header">
        <div><span className="eyebrow">RESEARCH OPERATIONS</span><h1>研究总览</h1></div>
        <button className="primary-button" onClick={onReview}>处理审核待办<ArrowRight size={16} /></button>
      </header>
      {summary.isError && <div className="error-banner">无法读取总览：{String(summary.error)}</div>}
      <div className="metric-grid metric-grid-six">
        <Metric icon={Clock3} label="审核待办" value={value?.actionable_posts ?? value?.candidate_posts ?? '-'} tone="amber" />
        <Metric icon={Radio} label="自动复核中" value={value?.classification_pending ?? '-'} tone="blue" />
        <Metric icon={Search} label="待确认线索" value={value?.pending_stock_leads ?? '-'} tone="neutral" />
        <Metric icon={CheckCircle2} label="已确认线索" value={value?.confirmed_stock_leads ?? '-'} tone="green" />
        <Metric icon={Database} label="活跃数据标的" value={value?.market?.active_instruments ?? '-'} tone="blue" />
        <Metric icon={Database} label="数据覆盖" value={value?.market?.coverage_count ?? '-'} tone="neutral" />
      </div>
      <div className="content-grid overview-grid">
        <div className="panel">
          <div className="panel-heading"><div><h2>正式事件跟踪</h2><span>{active.length} 条活跃事件</span></div></div>
          <div className="table-scroll">
            <table>
              <thead><tr><th>事件</th><th>KOL</th><th>标的</th><th>方向收益</th><th>超额</th><th>下一节点</th><th>警告</th></tr></thead>
              <tbody>
                {active.map((event) => (
                  <tr key={event.event_id}>
                    <td className="mono">{event.event_id}</td><td>{event.kol_name}</td>
                    <td><strong>{event.symbol}</strong> {event.security_name}</td>
                    <td className="return-value">{percent(event.latest_mark?.directional_return)}</td>
                    <td className="return-value">{percent(event.latest_mark?.directional_excess_return)}</td>
                    <td><span className="badge neutral">{nextCheckpoint(event.latest_mark?.tracking_days)}</span></td>
                    <td>{event.execution_warning ? <span className="warning-text"><AlertTriangle size={14} />{event.execution_warning}</span> : '-'}</td>
                  </tr>
                ))}
                {!active.length && <tr><td colSpan={7} className="empty-cell">暂无活跃事件</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
        <div className="panel run-panel">
          <div className="panel-heading"><div><h2>最近采集</h2><span>每日 19:00</span></div></div>
          {value?.last_fetch_run ? (
            <dl className="run-details">
              <div><dt>状态</dt><dd><span className={`badge ${value.last_fetch_run.status === 'success' ? 'green' : 'amber'}`}>{value.last_fetch_run.status}</span></dd></div>
              <div><dt>开始时间</dt><dd>{formatDate(value.last_fetch_run.started_at)}</dd></div>
              <div><dt>成功账号</dt><dd>{value.last_fetch_run.successful_kols}</dd></div>
              <div><dt>新增帖子</dt><dd>{value.last_fetch_run.new_posts}</dd></div>
              <div><dt>候选帖子</dt><dd>{value.last_fetch_run.candidate_posts}</dd></div>
            </dl>
          ) : <div className="empty-state">尚未运行自动采集</div>}
          <div className="scope-note"><AlertTriangle size={15} />每日采集无法恢复任务执行前已经删除的帖子。</div>
        </div>
      </div>
    </section>
  )
}

function nextCheckpoint(value?: string): string {
  const days = Number(value || 0)
  const target = [5, 20, 60, 120].find((item) => item > days)
  if (!target) return '已完成'
  const label = target === 5 ? '1W' : target === 20 ? '1M' : target === 60 ? '3M' : '6M'
  return `${label} / ${target - days}日`
}

function Metric({ icon: Icon, label, value, tone }: { icon: typeof Clock3; label: string; value: string | number; tone: string }) {
  return <div className="metric"><div className={`metric-icon ${tone}`}><Icon size={18} /></div><div><span>{label}</span><strong>{value}</strong></div></div>
}

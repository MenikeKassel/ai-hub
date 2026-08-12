import { useQuery } from '@tanstack/react-query'
import { Bot, CalendarCheck, CandlestickChart, RadioTower } from 'lucide-react'
import { api, formatDate } from '../api'

function shortTime(value?: string): string {
  if (!value) return '尚无记录'
  return formatDate(value)
}

const deliveryLabels: Record<string, string> = {
  pending: '等待',
  ready: '准时',
  degraded: '降级完成',
  late: '延迟',
  missed: '未交付',
}

export default function PipelineStatusBar() {
  const status = useQuery({
    queryKey: ['pipeline-status'],
    queryFn: api.pipelineStatus,
    refetchInterval: 60_000,
  })
  const value = status.data
  const delivery = value?.morning_delivery
  const marketClosed = value?.market_status === 'closed'
  const marketInSession = value?.market_status === 'trading' || value?.market_status === 'pre_open'
  const marketDetail = value?.market_status === 'trading'
    ? `交易中 · 日线截至 ${value?.latest_trade_date || '待同步'}`
    : value?.market_status === 'pre_open'
      ? `开盘前 · 日线截至 ${value?.latest_trade_date || '待同步'}`
      : marketClosed
    ? `休市 · 最新 ${value?.latest_trade_date || '待同步'}`
    : value?.lagging_symbols?.length
      ? `${value.lagging_symbols.length} 只标的滞后`
      : `最新 ${value?.latest_trade_date || '待同步'}`
  const aiQueueLabel = value
    ? `${value.queue_kind === 'next_preview' ? '下一晨报' : '今日'} ${value.queue_review_date} · 待 AI ${value.pending_ai} · 人工待审 ${value.pending_human_review}${value.failed_ai ? ` · 失败 ${value.failed_ai}` : ''}`
    : '尚无队列状态'
  const deliveryProgress = delivery && !delivery.completed_at && delivery.latest_run_id
    ? `${delivery.latest_phase || 'pipeline'} · ${delivery.stage || delivery.latest_status} · ${delivery.progress_current}/${delivery.progress_total}`
    : ''
  const platformSummary = delivery?.platform_breakdown
    ? Object.entries(delivery.platform_breakdown).map(([platform, item]) => `${platform.toUpperCase()} ${item.success}/${item.target}`).join(' · ')
    : ''

  return <div className="pipeline-status-bar" aria-label="数据流水线状态">
    <div><RadioTower size={15} /><span><small>最近帖子采集</small><strong>{shortTime(value?.latest_fetch_at)}</strong></span><i className={value?.latest_fetch_status === 'success' ? 'ok' : 'warn'} /></div>
    <div title={`最近 AI：${shortTime(value?.latest_ai_at)}`}><Bot size={15} /><span><small>AI 队列</small><strong title={aiQueueLabel}>{aiQueueLabel}</strong></span><i className={value?.pending_ai || value?.failed_ai ? 'warn' : 'ok'} /></div>
    <div><CalendarCheck size={15} /><span><small>晨报交付</small><strong title={`${deliveryProgress} ${platformSummary}`}>{delivery?.completed_at ? `${deliveryLabels[delivery.status] || delivery.status} · ${shortTime(delivery.completed_at)}` : deliveryProgress || deliveryLabels[delivery?.status || ''] || '尚未交付'}</strong></span><i className={delivery?.status === 'ready' ? 'ok' : 'warn'} /></div>
    <div><CandlestickChart size={15} /><span><small>行情交易日</small><strong>{marketDetail}</strong></span><i className={marketClosed || marketInSession || !value?.lagging_symbols?.length ? 'ok' : 'warn'} /></div>
  </div>
}

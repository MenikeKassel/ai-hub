import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Check, RefreshCw, X } from 'lucide-react'
import { api, formatDate } from '../api'
import PostDetail from '../features/reviews/ReviewDetail'
import type { ReviewView } from '../features/reviews/types'
import QueryState from '../components/QueryState'
import { shanghaiDate, updateRoute, useRoute } from '../workspace'

const viewKeys: ReviewView[] = ['new', 'processed', 'pending', 'failed', 'approved']
const viewLabels: Record<ReviewView, string> = { new: '新增帖子', processed: 'AI 已处理', pending: '待你确认', failed: 'AI 失败', approved: '今日批准' }
const deliveryLabels: Record<string, string> = { pending: '等待交付', ready: '准时完成', degraded: '降级完成', late: '延迟完成', missed: '未按时交付' }
const failureLabels: Record<string, string> = { authentication: '登录失效', budget: '预算耗尽', rate_limit: '触发限流', ocr: 'OCR 失败', model: '模型失败' }

export default function ReviewQueue() {
  const client = useQueryClient()
  const { params } = useRoute()
  const today = shanghaiDate()
  const nextMorning = shanghaiDate(1)
  const hasLegacyScope = params.has('scope')
  const routeDate = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date') || '') ? params.get('date')! : ''
  const reviewDate = routeDate === nextMorning ? nextMorning : today
  const view: ReviewView = viewKeys.includes(params.get('view') as ReviewView) ? params.get('view') as ReviewView : 'pending'
  const page = Math.max(1, Number(params.get('page')) || 1)
  const attentionOnly = params.get('attention') === '1'
  const queryParams = new URLSearchParams({ review_date: reviewDate, scope: 'morning', view, page: String(page), page_size: '50', attention_only: String(attentionOnly) })
  const query = useQuery({ queryKey: ['review-queue', queryParams.toString()], queryFn: ({ signal }) => api.reviewQueue(queryParams, signal), refetchInterval: 30_000 })
  const items = query.data?.items || []
  const selectedId = params.get('post') || items[0]?.post_id || ''
  const detail = useQuery({ queryKey: ['review-detail', selectedId], queryFn: ({ signal }) => api.reviewQueueDetail(selectedId, signal, reviewDate), enabled: Boolean(selectedId) })
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof api.bulkPreviewRecommendationDrafts>> | null>(null)
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.bulkApproveRecommendationDrafts>> | null>(null)
  const refresh = () => {
    client.invalidateQueries({ queryKey: ['review-queue'] })
    client.invalidateQueries({ queryKey: ['review-detail', selectedId] })
    client.invalidateQueries({ queryKey: ['pipeline-status'] })
    client.invalidateQueries({ queryKey: ['events'] })
  }
  const runMorning = useMutation({ mutationFn: () => api.startTask('morning'), onSettled: refresh })
  const bulkPreview = useMutation({ mutationFn: () => api.bulkPreviewRecommendationDrafts(reviewDate), onSuccess: (value) => { setPreview(value); setResult(null) } })
  const bulkApprove = useMutation({ mutationFn: (token: string) => api.bulkApproveRecommendationDrafts(token, '确认晨报预览中的可批准草稿'), onSuccess: (value) => { setResult(value); setPreview(null); refresh() } })
  const canBulk = reviewDate === today && view === 'pending' && !attentionOnly
  useEffect(() => { setPreview(null); setResult(null) }, [reviewDate, view, attentionOnly, page])
  useEffect(() => {
    if (hasLegacyScope || (routeDate && routeDate !== today && routeDate !== nextMorning)) {
      updateRoute({ date: reviewDate, scope: null, page: 1, post: null, detail: null, attention: null }, true)
    }
  }, [hasLegacyScope, nextMorning, reviewDate, routeDate, today])
  const delivery = query.data?.delivery
  const deliveryErrors = delivery?.errors || []
  const deliveryPending = Object.values(delivery?.platform_breakdown || {}).reduce((total, platform) => total + platform.pending, 0)
  const switchQueue = (date: string) => updateRoute({ date, scope: null, page: 1, post: null, detail: null, attention: null })
  const sourceItem = detail.data ? { postId: selectedId, post: detail.data.post, drafts: detail.data.drafts, historical: false } : null

  return <section className={`review-workspace ${params.get('detail') === '1' ? 'show-review-detail' : ''}`}>
    <header className="page-header"><div><span className="eyebrow">REVIEW WORKSPACE</span><h1>审核工作台</h1><p className="page-description">读原文，核对证据，再确认每一条观点。</p></div><button className="secondary-button" disabled={runMorning.isPending} onClick={() => runMorning.mutate()}><RefreshCw size={16} />{runMorning.isPending ? '正在启动…' : '运行完整晨报'}</button></header>
    <div className="review-scope-bar"><h2>{reviewDate === today ? '今日审核' : '下一晨报'}</h2><nav className="segmented" aria-label="审核范围"><button className={reviewDate === today ? 'active' : ''} onClick={() => switchQueue(today)}>今日审核</button><button className={reviewDate === nextMorning ? 'active' : ''} onClick={() => switchQueue(nextMorning)}>下一晨报</button></nav></div>
    {delivery && <div className={`delivery-strip ${delivery.status}`}><strong>09:00 晨报交付快照 · <span className="delivery-status">{deliveryLabels[delivery.status] || delivery.status}</span></strong><span>{delivery.successful_kols} / {delivery.active_kols} 个账号完成采集 · 覆盖率 {delivery.coverage == null ? '未知' : `${Math.round(delivery.coverage * 100)}%`} · 未尝试 {deliveryPending} · 失败 {delivery.failed_kols}</span><details><summary>交付详情{deliveryErrors.length ? ` · ${deliveryErrors.length} 项异常` : ''}</summary><p>这是当次晨报的结算记录，之后的补抓不会改写它。</p><p>{delivery.completed_at ? `阶段完成：${formatDate(delivery.completed_at)}` : '阶段尚未完成'}</p>{Object.entries(delivery.platform_breakdown || {}).map(([platform, value]) => <p key={platform}>{platform.toUpperCase()}：成功 {value.success} / 目标 {value.target} · 失败 {value.failed} · 等待 {value.pending}</p>)}{!Object.keys(delivery.platform_breakdown || {}).length && <p>这次记录缺少分平台统计。</p>}{deliveryErrors.map((error, index) => <p key={index}>{error}</p>)}</details></div>}
    <div className="review-counters">{viewKeys.map((key) => <button key={key} aria-pressed={view === key} className={view === key ? 'active' : ''} onClick={() => updateRoute({ view: key, page: 1, post: null, detail: null, attention: null })}><span>{viewLabels[key]}</span><strong>{query.data?.counts?.[key] ?? '—'}<small>篇</small></strong></button>)}</div>
    <div className="queue-toolbar"><div><strong>{reviewDate} 晨报</strong><span>共 {query.data?.total_posts ?? '—'} 篇帖子 · {query.data?.total_drafts ?? '—'} 条草稿</span></div><div className="header-actions">{view === 'pending' && <label className="checkbox-inline"><input type="checkbox" checked={attentionOnly} onChange={(event) => updateRoute({ attention: event.target.checked ? '1' : null, page: 1, post: null })} />只看需核对</label>}{canBulk && <button className="primary-button" disabled={bulkPreview.isPending} onClick={() => bulkPreview.mutate()}>预览批量批准</button>}<button className="icon-button" title="刷新队列" onClick={refresh}><RefreshCw size={16} /></button></div></div>
    <QueryState loading={query.isLoading} error={query.error || runMorning.error || bulkPreview.error || bulkApprove.error} stale={Boolean(query.data)} />
    {preview && <div className="panel bulk-approval-panel"><div className="panel-heading"><h2>批量批准预览 · {preview.count} 条</h2><button className="icon-button" title="关闭预览" onClick={() => setPreview(null)}><X size={16} /></button></div><p>仅包含当前晨报全部可批准项，最多 200 条。快照有效至 {formatDate(preview.expires_at)}。</p><div className="bulk-approval-list">{preview.drafts.map((draft) => <span key={draft.id} className="badge neutral">{draft.symbol} {draft.security_name}</span>)}</div><div className="review-actions"><button className="secondary-button" onClick={() => setPreview(null)}>取消</button><button className="primary-button" disabled={bulkApprove.isPending || !preview.count} onClick={() => bulkApprove.mutate(preview.snapshot_token)}><Check size={16} />确认批量批准</button></div></div>}
    {result && <div className="notice-banner">批准 {result.approved.length} 条 · 跳过 {result.skipped.length} 条 · 失败 {result.failed.length} 条</div>}
    <div className="review-layout compact-review-layout"><aside className="post-list" aria-label="审核帖子列表">{items.map((item) => <button className={`post-item ${item.post_id === selectedId ? 'selected' : ''}`} key={item.post_id} onClick={() => updateRoute({ post: item.post_id, detail: '1' })}><div className="post-item-heading"><strong>{item.display_name || item.handle}</strong><span className="badge neutral">{item.platform}</span></div><small>{formatDate(item.posted_at)}</small><p>{item.excerpt || '图片或长文帖子'}</p><div className="post-item-footer"><span>{item.draft_count} 条草稿</span>{item.attention_count > 0 && <span className="badge amber">{item.attention_count} 条需核对</span>}{item.failure_kind && <span className="badge red">{failureLabels[item.failure_kind]}</span>}</div></button>)}{!query.isLoading && !items.length && <div className="empty-state"><strong>当前范围暂无记录</strong><span>可以切换今日审核或下一晨报。</span></div>}</aside><div className="review-detail"><button className="secondary-button detail-back" onClick={() => updateRoute({ detail: null })}><ArrowLeft size={16} />返回列表</button><QueryState loading={detail.isLoading && Boolean(selectedId)} error={detail.error} stale={Boolean(detail.data)} />{sourceItem && <PostDetail key={selectedId} item={sourceItem} reviewDate={reviewDate} onDone={refresh} />}{!selectedId && <div className="empty-state">选择一篇帖子查看原文与草稿</div>}</div></div>
    <footer className="queue-pagination"><span>当前页 {items.length} 篇 · 每页 50 篇</span><div><button className="icon-button" title="上一页" disabled={page <= 1} onClick={() => updateRoute({ page: page - 1, post: null, detail: null })}><ArrowLeft size={16} /></button><span>第 {page} 页</span><button className="icon-button" title="下一页" disabled={!query.data?.has_more} onClick={() => updateRoute({ page: page + 1, post: null, detail: null })}><ArrowRight size={16} /></button></div></footer>
  </section>
}

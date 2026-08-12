import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Check, CheckCircle2, ExternalLink, FileText, Image as ImageIcon,
  Plus, RefreshCw, Sparkles, X, XCircle,
} from 'lucide-react'
import { api, formatDate } from '../api'
import ActionToast from '../components/ActionToast'
import type { DraftCorrectionType, Post, RecommendationDraft } from '../types'

type MorningView = 'new' | 'processed' | 'pending' | 'failed' | 'approved'
type PendingFilter = 'all' | 'attention'

interface ReviewItem {
  postId: string
  post?: Post
  drafts: RecommendationDraft[]
  historical: boolean
}

const today = () => new Date().toLocaleDateString('en-CA')
const tomorrow = () => { const value = new Date(); value.setDate(value.getDate() + 1); return value.toLocaleDateString('en-CA') }

const attentionLabels: Record<string, string> = {
  instrument_not_found: '证券代码未映射',
  invalid_symbol: '股票代码无效',
  missing_thesis: '缺少推荐理由',
  missing_evidence: '缺少原文证据',
  evidence_not_found: '证据无法在原帖定位',
  image_dependency: '依赖图片或 OCR',
  reply_context_required: '缺少回复上下文',
  source_conflict: '来源时间冲突',
  posted_at_timezone: '发布时间缺少时区',
  invalid_action: '态度动作无效',
  invalid_horizon: '持有周期无效',
  invalid_strength: '推荐强度无效',
  secondhand_source: '转发或汇总来源，不能注册为直接 KOL 事件',
}

const actionLabels = { buy: '买入', add: '加仓', hold: '持有', watch: '关注', reduce: '减仓', sell: '卖出', avoid: '回避' } as const
const horizonLabels = { intraday: '日内', short: '短线', swing: '波段', medium_long: '中长线', unspecified: '未说明' } as const
const strengthLabels = { explicit: '明确推荐', moderate: '一般关注', weak: '弱提示', unspecified: '未说明' } as const
const correctionLabels: Record<DraftCorrectionType, string> = {
  missed_stock: '漏识别股票', wrong_mapping: '股票映射错误', wrong_direction: '方向错误',
  wrong_thesis: '理由错误', wrong_evidence: '证据错误', wrong_content_type: '内容类型错误',
}
const deliveryLabels = { pending: '等待交付', ready: '准时完成', degraded: '降级完成', late: '延迟完成', missed: '未按时交付' } as const

const viewMeta: Record<MorningView, { label: string; description: string; icon: typeof FileText }> = {
  new: { label: '新增帖子', description: '当天采集到的全部原帖，包括尚未完成 AI 处理的内容。', icon: FileText },
  processed: { label: 'AI 已处理', description: '已完成分类的帖子；没有草稿表示 AI 判定它不是事前荐股。', icon: Sparkles },
  pending: { label: '待你确认', description: 'AI 已逐只股票填好代码、方向、理由和证据，等待你作最终决定。', icon: AlertTriangle },
  failed: { label: 'AI 失败', description: 'OCR、模型或结构化处理失败的帖子，会由后续任务重试。', icon: XCircle },
  approved: { label: '今日批准', description: '今天经你批准并进入正式收益跟踪的推荐草稿。', icon: CheckCircle2 },
}

function groupDrafts(drafts: RecommendationDraft[]): ReviewItem[] {
  const grouped = new Map<string, RecommendationDraft[]>()
  drafts.forEach((draft) => grouped.set(draft.post_id, [...(grouped.get(draft.post_id) || []), draft]))
  return [...grouped.entries()]
    .map(([postId, values]) => ({ postId, drafts: values, historical: values.every((draft) => draft.queue_scope === 'backlog') }))
    .sort((left, right) => {
      if (left.historical !== right.historical) return left.historical ? 1 : -1
      return (right.drafts[0]?.posted_at || '').localeCompare(left.drafts[0]?.posted_at || '')
    })
}

function evidenceLines(value: string): string[] {
  return value.split('\n').map((item) => item.trim()).filter(Boolean)
}

function DraftEditor({ draft, sourceText, onDone }: { draft: RecommendationDraft; sourceText: string; onDone: () => void }) {
  const [symbol, setSymbol] = useState(draft.symbol)
  const [name, setName] = useState(draft.security_name)
  const [direction, setDirection] = useState(draft.direction)
  const [action, setAction] = useState(draft.action || 'watch')
  const [horizon, setHorizon] = useState(draft.horizon || 'unspecified')
  const [strength, setStrength] = useState(draft.strength || 'unspecified')
  const [thesis, setThesis] = useState(draft.thesis)
  const [evidenceText, setEvidenceText] = useState(draft.evidence_spans.join('\n'))
  const [note, setNote] = useState('')
  const [correctionType, setCorrectionType] = useState<DraftCorrectionType | ''>('')
  const [lastAction, setLastAction] = useState<'approve' | 'reject' | 'retry'>('approve')
  const evidence = evidenceLines(evidenceText)
  const missingEvidence = evidence.length === 0
  const evidenceNotFound = evidence.some((item) => !sourceText.includes(item))
  const evidenceChanged = evidenceText !== draft.evidence_spans.join('\n')
  const fieldsChanged = symbol !== draft.symbol || name !== draft.security_name || direction !== draft.direction || action !== (draft.action || 'watch') || horizon !== (draft.horizon || 'unspecified') || strength !== (draft.strength || 'unspecified') || thesis !== draft.thesis || evidenceChanged
  const locallyResolved = new Set<string>()
  if (thesis.trim()) locallyResolved.add('missing_thesis')
  if (/^\d{6}$/.test(symbol)) locallyResolved.add('invalid_symbol')
  if (/^\d{6}$/.test(symbol) && symbol !== draft.symbol) locallyResolved.add('instrument_not_found')
  if (!missingEvidence) locallyResolved.add('missing_evidence')
  if (!evidenceNotFound) locallyResolved.add('evidence_not_found')
  const hardBlockers = draft.attention_reasons.filter((item) => item !== 'image_dependency' && !locallyResolved.has(item))
  const canSubmit = /^\d{6}$/.test(symbol) && thesis.trim() && !missingEvidence && !evidenceNotFound && hardBlockers.length === 0 && (!fieldsChanged || Boolean(correctionType))
  const mutation = useMutation({
    mutationFn: async (command: 'approve' | 'reject' | 'retry') => {
      if (command === 'retry') return api.retryRecommendationDraft(draft.id)
      if (command === 'reject') return api.rejectRecommendationDraft(draft.id, note)
      const updated = await api.patchRecommendationDraft(draft.id, {
        symbol, security_name: name, direction, action, horizon, strength, thesis, evidence_spans: evidence, note,
        correction_type: correctionType || undefined,
      })
      const remaining = updated.attention_reasons.filter((item) => item !== 'image_dependency')
      if (remaining.length) return updated
      return api.approveRecommendationDraft(draft.id, note)
    },
    onSuccess: onDone,
  })
  const completed = draft.status === 'approved' || draft.status === 'rejected'
  const message = mutation.isPending
    ? lastAction === 'approve' ? '正在校验证据并注册事件…' : '正在保存审核结果…'
    : mutation.isSuccess
      ? lastAction === 'approve' && mutation.data && 'status' in mutation.data && mutation.data.status !== 'approved'
        ? `修改已保存，仍需处理：${mutation.data.attention_reasons.map((item) => attentionLabels[item] || item).join('；')}`
        : '审核结果已保存'
      : mutation.isError ? String(mutation.error) : ''

  return <article className={`recommendation-draft ${draft.status}`}>
    <div className="draft-heading">
      <div><strong>{draft.symbol} {draft.security_name}</strong><span>{draft.status === 'needs_attention' ? '需要核对' : draft.status === 'approved' ? '已批准' : draft.status === 'rejected' ? '已排除' : '待确认'}</span></div>
      <span className={`badge ${draft.status === 'approved' ? 'green' : draft.status === 'rejected' ? 'red' : draft.status === 'needs_attention' ? 'amber' : 'blue'}`}>{Math.round(draft.confidence * 100)}%</span>
    </div>
    <div className="draft-editor-grid">
      <label>股票代码<input value={symbol} disabled={completed} onChange={(event) => setSymbol(event.target.value.replace(/\D/g, '').slice(0, 6))} /></label>
      <label>股票名称<input value={name} disabled={completed} onChange={(event) => setName(event.target.value)} /></label>
      <label>方向<select value={direction} disabled={completed} onChange={(event) => setDirection(event.target.value as 'long' | 'short')}><option value="long">看多</option><option value="short">看空</option></select></label>
      <label>动作<select value={action} disabled={completed} onChange={(event) => setAction(event.target.value as keyof typeof actionLabels)}>{Object.entries(actionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>周期<select value={horizon} disabled={completed} onChange={(event) => setHorizon(event.target.value as keyof typeof horizonLabels)}>{Object.entries(horizonLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>强度<select value={strength} disabled={completed} onChange={(event) => setStrength(event.target.value as keyof typeof strengthLabels)}>{Object.entries(strengthLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="wide">推荐理由<textarea value={thesis} disabled={completed} onChange={(event) => setThesis(event.target.value)} /></label>
    </div>
    <label className={`draft-evidence-editor ${missingEvidence || evidenceNotFound ? 'invalid' : ''}`}>
      <span>原文证据（每行一条，必须逐字出现在正文或 OCR 中）</span>
      <textarea aria-label="原文证据" value={evidenceText} disabled={completed} onChange={(event) => setEvidenceText(event.target.value)} />
      {!completed && missingEvidence && <small>至少保留一条可以核对的原文。</small>}
      {!completed && !missingEvidence && evidenceNotFound && <small>当前证据不是原文逐字引用，请从上方正文或 OCR 结果中复制。</small>}
    </label>
    {draft.conditions.length > 0 && <div className="draft-evidence"><span>条件</span><p>{draft.conditions.join('；')}</p></div>}
    {draft.attention_reasons.length > 0 && <div className="attention-list"><AlertTriangle size={15} />{draft.attention_reasons.map((item) => <span key={item}>{attentionLabels[item] || item}</span>)}</div>}
    {!completed && <>
      {(hardBlockers.length > 0 || missingEvidence || evidenceNotFound) && <div className="draft-blocker-panel"><strong>暂不能批准</strong><span>{missingEvidence ? '缺少原文证据' : evidenceNotFound ? '证据无法在正文或 OCR 中逐字定位' : hardBlockers.map((item) => attentionLabels[item] || item).join('；')}</span></div>}
      {fieldsChanged && <label className="note-field">AI 错误类型<select aria-label="AI 错误类型" value={correctionType} onChange={(event) => setCorrectionType(event.target.value as DraftCorrectionType)}><option value="">请选择</option>{Object.entries(correctionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
      <label className="note-field">审核备注<input value={note} onChange={(event) => setNote(event.target.value)} placeholder="可选" /></label>
      <div className="review-actions">
        <button className="icon-button" title="重新分析" disabled={mutation.isPending} onClick={() => { setLastAction('retry'); mutation.mutate('retry') }}><RefreshCw size={16} /></button>
        <button className="danger-button" disabled={mutation.isPending} onClick={() => { setLastAction('reject'); mutation.mutate('reject') }}><X size={16} />排除</button>
        <button className="primary-button" disabled={mutation.isPending || !canSubmit} onClick={() => { setLastAction('approve'); mutation.mutate('approve') }}><Check size={16} />{!canSubmit ? fieldsChanged && !correctionType ? '选择错误类型' : '先修复证据' : fieldsChanged ? '保存并批准跟踪' : draft.status === 'needs_attention' ? '重新校验并批准' : '批准并跟踪'}</button>
      </div>
    </>}
    {message && <ActionToast state={mutation.isPending ? 'pending' : mutation.isError ? 'error' : 'success'} message={message} />}
  </article>
}

function ManualDraftForm({ postId, reviewDate, sourceText, onDone, onCancel }: { postId: string; reviewDate: string; sourceText: string; onDone: () => void; onCancel: () => void }) {
  const [form, setForm] = useState({ symbol: '', security_name: '', direction: 'long' as const, action: 'watch' as const, horizon: 'unspecified' as const, strength: 'explicit' as const, thesis: '', evidence: '', note: '' })
  const [evidenceSource, setEvidenceSource] = useState<'text' | 'ocr'>('text')
  const evidence = evidenceLines(form.evidence)
  const evidenceValid = evidence.length > 0 && evidence.every((value) => sourceText.includes(value))
  const mutation = useMutation({
    mutationFn: () => api.createManualRecommendationDraft(postId, {
      symbol: form.symbol, security_name: form.security_name, direction: form.direction,
      action: form.action, horizon: form.horizon, strength: form.strength, thesis: form.thesis,
      evidence_spans: evidence, conditions: [], evidence_source: evidenceSource, depends_on_ocr: evidenceSource === 'ocr',
      review_date: reviewDate, correction_type: 'missed_stock', note: form.note,
    }),
    onSuccess: onDone,
  })
  const valid = /^\d{6}$/.test(form.symbol) && form.security_name.trim() && form.thesis.trim() && evidenceValid && form.note.trim()
  return <section className="manual-draft-form">
    <div className="section-heading"><div><span className="eyebrow">MANUAL CORRECTION</span><h3>补建遗漏的推荐草稿</h3></div><button className="icon-button" title="关闭" onClick={onCancel}><X size={15} /></button></div>
    <div className="draft-editor-grid">
      <label>股票代码<input aria-label="补建股票代码" value={form.symbol} onChange={(event) => setForm({ ...form, symbol: event.target.value.replace(/\D/g, '').slice(0, 6) })} /></label>
      <label>股票名称<input aria-label="补建股票名称" value={form.security_name} onChange={(event) => setForm({ ...form, security_name: event.target.value })} /></label>
      <label>方向<select value={form.direction} onChange={(event) => setForm({ ...form, direction: event.target.value as 'long' })}><option value="long">看多</option><option value="short">看空</option></select></label>
      <label>证据来源<select aria-label="补建证据来源" value={evidenceSource} onChange={(event) => setEvidenceSource(event.target.value as 'text' | 'ocr')}><option value="text">帖子正文</option><option value="ocr">本地 OCR</option></select></label>
      <label className="wide">推荐理由<textarea aria-label="补建推荐理由" value={form.thesis} onChange={(event) => setForm({ ...form, thesis: event.target.value })} /></label>
    </div>
    <label className={`draft-evidence-editor ${form.evidence && !evidenceValid ? 'invalid' : ''}`}><span>原文证据（逐字复制，每行一条）</span><textarea aria-label="补建原文证据" value={form.evidence} onChange={(event) => setForm({ ...form, evidence: event.target.value })} />{form.evidence && !evidenceValid && <small>证据必须能在正文或 OCR 中逐字定位。</small>}</label>
    <label className="note-field">纠错说明<input aria-label="补建纠错说明" value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="说明 AI 漏掉了什么" /></label>
    <div className="review-actions"><button className="secondary-button" onClick={onCancel}>取消</button><button className="primary-button" disabled={mutation.isPending || !valid} onClick={() => mutation.mutate()}><Plus size={16} />保存草稿</button></div>
    {mutation.isError && <div className="error-banner">补建失败：{String(mutation.error)}</div>}
  </section>
}

function PostDetail({ item, reviewDate, onDone }: { item: ReviewItem; reviewDate: string; onDone: () => void }) {
  const [showManual, setShowManual] = useState(false)
  const reprocess = useMutation({
    mutationFn: () => api.reprocessRecommendationPost(item.postId),
    onSuccess: onDone,
  })
  const source = item.post || item.drafts[0]
  if (!source) return <div className="empty-state">这条记录没有可展示的来源快照</div>
  const post = item.post
  const ocrText = source.ocr_text || ''
  const sourceText = [source.text, source.article_text, source.quoted_text, ocrText].filter(Boolean).join('\n')
  const showOcr = item.drafts.some((draft) => draft.depends_on_ocr || draft.attention_reasons.some((reason) => reason === 'image_dependency' || reason === 'evidence_not_found'))
  const isHistoricalArchive = post?.review_status === 'ignored' && post.review_note === 'Zhihu historical backfill is archive-only.'
  const modelStatus = isHistoricalArchive ? '历史归档' : post?.model_status === 'completed' ? '已完成' : post?.model_status === 'failed' ? '失败' : post?.model_status === 'running' ? '处理中' : '等待处理'

  return <>
    <div className="source-heading"><div><span className="eyebrow">{source.platform === 'Zhihu' ? source.handle : `@${source.handle}`} <b className={`badge ${source.platform === 'Zhihu' ? 'blue' : 'neutral'}`}>{source.platform === 'Zhihu' ? '知乎' : 'X'}</b>{item.historical && <b className="badge amber">历史修复</b>}</span><h2>{source.display_name}</h2><small>{formatDate(source.posted_at)} · {source.model_name || '等待模型'}</small></div><div className="header-actions">{item.drafts.length === 0 && <button className="secondary-button" disabled={reprocess.isPending} onClick={() => reprocess.mutate()}><RefreshCw size={15} />重新生成推荐</button>}<button className="secondary-button" onClick={() => setShowManual(!showManual)}><Plus size={15} />补建草稿</button><a className="icon-button" href={source.url} target="_blank" rel="noreferrer" title="打开原帖"><ExternalLink size={16} /></a></div></div>
    {post && <div className="classification-band morning-classification">
      <div><span>AI 状态</span><strong>{modelStatus}</strong></div>
      <div><span>内容类型</span><strong>{post.content_type || '待判断'}</strong></div>
      <div><span>证据类型</span><strong>{post.evidence_type || '待判断'}</strong></div>
      <div><span>候选荐股</span><strong>{post.is_candidate ? '是' : '否'}</strong></div>
    </div>}
    <div className="source-text">{source.text}</div>
    {source.article_text && <div className="source-text secondary"><strong>{source.article_title}</strong>{source.article_text}</div>}
    {source.quoted_text && <div className="source-text secondary">引用：{source.quoted_text}</div>}
    {source.local_media.length > 0 && <div className="media-grid">{source.local_media.map((media) => <a key={media.sha256} href={media.api_url} target="_blank" rel="noreferrer"><img src={media.api_url} alt="帖子图片快照" /><span><ImageIcon size={14} />查看原图</span></a>)}</div>}
    {ocrText && <details className="ocr-source" open={showOcr}><summary>本地 OCR 文字</summary><pre>{ocrText.replace(/<\|det\|>.*?<\|\/det\|>/g, '').trim()}</pre></details>}
    {post?.model_summary && <div className="model-summary"><strong>AI 摘要</strong><p>{post.model_summary}</p></div>}
    {post?.model_status === 'failed' && <div className="error-banner morning-source-error">AI 处理失败：{post.model_error || post.ocr_error || '未记录具体错误，等待下一轮重试。'}</div>}
    {showManual && <ManualDraftForm postId={item.postId} reviewDate={reviewDate} sourceText={sourceText} onCancel={() => setShowManual(false)} onDone={() => { setShowManual(false); onDone() }} />}
    {item.drafts.length > 0
      ? <div className="recommendation-draft-list">{item.drafts.map((draft) => <DraftEditor key={draft.id} draft={draft} sourceText={sourceText} onDone={onDone} />)}</div>
      : <div className="empty-state morning-empty-detail">{isHistoricalArchive
         ? '这是启用监控前的知乎历史回答：仅归档和搜索，不进入晨报审核、正式事件或收益统计。'
         : post?.model_status === 'completed'
           ? `推荐草稿生成异常：AI 已完成分析（${post.content_type || '类型未知'} / ${post.evidence_type || '证据不足'}），但没有生成可审核草稿。可点击“重新生成推荐”或“补建草稿”。`
           : '这篇帖子尚未生成可审核的推荐草稿。'}</div>}
    {reprocess.isError && <div className="error-banner">推荐草稿重新生成失败：{String(reprocess.error)}</div>}
  </>
}

export default function ReviewQueue() {
  const client = useQueryClient()
  const [reviewDate, setReviewDate] = useState(today)
  const [view, setView] = useState<MorningView>('pending')
  const [pendingFilter, setPendingFilter] = useState<PendingFilter>('all')
  const [historyPage, setHistoryPage] = useState(1)
  const [selectedPostId, setSelectedPostId] = useState('')
  const [bulkPreview, setBulkPreview] = useState<Awaited<ReturnType<typeof api.bulkPreviewRecommendationDrafts>> | null>(null)
  const [bulkResult, setBulkResult] = useState<Awaited<ReturnType<typeof api.bulkApproveRecommendationDrafts>> | null>(null)
  const query = useQuery({
    queryKey: ['morning-review', reviewDate, historyPage],
    queryFn: () => api.morningReview(reviewDate, historyPage),
    refetchInterval: 30_000,
  })
  const runMorning = useMutation({
    mutationFn: () => api.startTask('morning'),
    onSettled: () => {
      client.invalidateQueries({ queryKey: ['morning-review'] })
      client.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
  })
  const bulkPreviewMutation = useMutation({
    mutationFn: () => api.bulkPreviewRecommendationDrafts(reviewDate, 'morning'),
    onSuccess: (value) => { setBulkPreview(value); setBulkResult(null) },
  })
  const bulkApproveMutation = useMutation({
    mutationFn: (token: string) => api.bulkApproveRecommendationDrafts(token, '批量批准当前晨报可批准草稿'),
    onSuccess: (value) => { setBulkResult(value); setBulkPreview(null); refresh() },
  })
  const canBulkApprove = reviewDate === today() && view === 'pending' && pendingFilter === 'all'
  const allDrafts = useMemo(() => {
    const unique = new Map<number, RecommendationDraft>()
    ;[...(query.data?.drafts || []), ...(query.data?.history_drafts || []), ...(query.data?.approved_drafts || [])].forEach((draft) => unique.set(draft.id, draft))
    return [...unique.values()]
  }, [query.data?.approved_drafts, query.data?.drafts, query.data?.history_drafts])
  const draftsByPost = useMemo(() => {
    const values = new Map<string, RecommendationDraft[]>()
    allDrafts.forEach((draft) => values.set(draft.post_id, [...(values.get(draft.post_id) || []), draft]))
    return values
  }, [allDrafts])
  const items = useMemo<ReviewItem[]>(() => {
    const posts = query.data?.posts || []
    if (view === 'new' || view === 'processed' || view === 'failed') {
      const selectedPosts = posts.filter((post) => view === 'new' || (view === 'processed' ? post.model_status === 'completed' : post.model_status === 'failed'))
      return selectedPosts.map((post) => ({ postId: post.post_id, post, drafts: draftsByPost.get(post.post_id) || [], historical: false }))
    }
    if (view === 'approved') return groupDrafts(query.data?.approved_drafts || [])
    const pending = [...(query.data?.drafts || []), ...(query.data?.history_drafts || [])].filter((draft) => draft.status === 'ready' || draft.status === 'needs_attention')
    return groupDrafts(pendingFilter === 'attention' ? pending.filter((draft) => draft.status === 'needs_attention') : pending)
  }, [draftsByPost, pendingFilter, query.data?.approved_drafts, query.data?.drafts, query.data?.history_drafts, query.data?.posts, view])

  useEffect(() => {
    if (!items.length) { setSelectedPostId(''); return }
    if (!items.some((item) => item.postId === selectedPostId)) setSelectedPostId(items[0].postId)
  }, [items, selectedPostId])
  useEffect(() => {
    setHistoryPage(1)
  }, [reviewDate])
  const selected = items.find((item) => item.postId === selectedPostId)
  const refresh = () => {
    client.invalidateQueries({ queryKey: ['morning-review'] })
    client.invalidateQueries({ queryKey: ['events'] })
    client.invalidateQueries({ queryKey: ['market'] })
    client.invalidateQueries({ queryKey: ['summary'] })
  }
  const summary = query.data?.summary
  const delivery = query.data?.delivery
  const counts: Record<MorningView, number | string> = {
    new: summary?.new_posts ?? '-',
    processed: summary?.ai_processed ?? '-',
    pending: summary?.waiting_review ?? '-',
    failed: summary?.ai_failed ?? '-',
    approved: summary?.approved_today ?? '-',
  }

  return <section>
    <header className="page-header">
      <div><span className="eyebrow">MORNING REVIEW</span><h1>今日审核</h1></div>
      <div className="header-actions review-date-controls"><button className="secondary-button" disabled={runMorning.isPending} onClick={() => runMorning.mutate()}><RefreshCw className={runMorning.isPending ? 'spin' : ''} size={15} />运行完整晨报</button><div className="segmented"><button className={reviewDate === today() ? 'active' : ''} onClick={() => setReviewDate(today())}>今日晨报</button><button className={reviewDate === tomorrow() ? 'active' : ''} onClick={() => setReviewDate(tomorrow())}>下一晨报预览</button></div><input className="date-input" type="date" value={reviewDate} onChange={(event) => setReviewDate(event.target.value)} /></div>
    </header>
    {delivery && <div className={`morning-delivery ${delivery.status}`}>
      <div><span>09:00 晨报</span><strong>{deliveryLabels[delivery.status]}</strong></div>
      <p>{delivery.active_kols > 0
        ? `${delivery.successful_kols} / ${delivery.active_kols} 个账号完成采集 · 覆盖率 ${(delivery.coverage * 100).toFixed(0)}%`
        : delivery.latest_run_id
          ? `${delivery.latest_phase || 'pipeline'} · ${delivery.stage || delivery.latest_status} · ${delivery.progress_current}/${delivery.progress_total}`
          : '完整晨报尚未启动；帖子补抓成功后仍需运行完整晨报。'}{delivery.completed_at ? ` · ${formatDate(delivery.completed_at)}` : ''}</p>
      {delivery.errors.length > 0 && <details className="morning-delivery-errors"><summary>{delivery.errors.length} 组异常，展开查看</summary><small>{delivery.errors.join('；')}</small></details>}
    </div>}
    <div className="metric-grid metric-grid-five morning-metrics" aria-label="晨间队列筛选">
      {(Object.keys(viewMeta) as MorningView[]).map((key) => {
        const Icon = viewMeta[key].icon
        return <button type="button" key={key} className={`metric metric-filter ${view === key ? 'active' : ''}`} aria-pressed={view === key} onClick={() => { setView(key); setSelectedPostId('') }}>
          <span className="metric-filter-icon"><Icon size={17} /></span><span><span>{viewMeta[key].label}</span><strong>{counts[key]}</strong></span>
        </button>
      })}
    </div>
    {view === 'pending' && <div className="bulk-approval-toolbar"><button className="primary-button" disabled={!canBulkApprove || bulkPreviewMutation.isPending} title={!canBulkApprove ? '仅允许对今日晨报的全部可批准草稿执行批量操作' : '先预览，再确认批量批准'} onClick={() => bulkPreviewMutation.mutate()}>批准当前可批准项</button>{reviewDate !== today() && <span className="secondary-line">批量批准仅开放给今日晨报</span>}{pendingFilter === 'attention' && <span className="secondary-line">异常筛选不会进入批量批准</span>}</div>}
    {bulkPreviewMutation.isError && <div className="error-banner">批量预览失败：{String(bulkPreviewMutation.error)}</div>}
    {bulkApproveMutation.isError && <div className="error-banner">批量批准失败：{String(bulkApproveMutation.error)}</div>}
    {bulkPreview && <div className="bulk-approval-panel panel">
      <div className="panel-heading"><div><h2>批量批准预览</h2><span>仅包含当前晨报中 ready 且无阻塞原因的草稿。</span></div><button className="icon-button" onClick={() => setBulkPreview(null)} title="关闭"><X size={15} /></button></div>
      <p><strong>{bulkPreview.count}</strong> 条草稿将逐条注册为正式事件。快照有效至 {formatDate(bulkPreview.expires_at)}。</p>
      <div className="bulk-approval-list">{bulkPreview.drafts.slice(0, 12).map((draft) => <span key={draft.id} className="badge neutral">{draft.symbol} {draft.security_name}</span>)}{bulkPreview.count > 12 && <span className="badge neutral">另有 {bulkPreview.count - 12} 条</span>}</div>
      <div className="review-actions"><button className="secondary-button" onClick={() => setBulkPreview(null)}>取消</button><button className="primary-button" disabled={bulkApproveMutation.isPending} onClick={() => bulkApproveMutation.mutate(bulkPreview.snapshot_token)}><Check size={15} />确认批量批准</button></div>
    </div>}
    {bulkResult && <div className="notice-banner">批量批准完成：成功 {bulkResult.approved.length}，跳过 {bulkResult.skipped.length}，失败 {bulkResult.failed.length}；行情刷新已{bulkResult.refresh_status === 'queued' ? '排队' : '无需排队'}。</div>}
    <div className="morning-view-bar">
      <span><strong>{viewMeta[view].label}</strong>{viewMeta[view].description}</span>
      {view === 'pending' && <div className="header-actions"><div className="segmented" role="tablist"><button className={pendingFilter === 'all' ? 'active' : ''} onClick={() => setPendingFilter('all')}>全部草稿</button><button className={pendingFilter === 'attention' ? 'active' : ''} onClick={() => setPendingFilter('attention')}>只看异常</button></div><div className="segmented" aria-label="历史修复分页"><button className="icon-button" title="上一页历史修复" disabled={historyPage <= 1} onClick={() => setHistoryPage((value) => Math.max(1, value - 1))}>‹</button><span className="pagination-label">历史第 {historyPage} 页</span><button className="icon-button" title="下一页历史修复" disabled={!query.data?.history_has_more} onClick={() => setHistoryPage((value) => value + 1)}>›</button></div></div>}
      <b>{items.length} 篇帖子{view === 'pending' || view === 'approved' ? ` · ${items.reduce((total, item) => total + item.drafts.length, 0)} 条草稿` : ''}</b>
    </div>
    {query.isError && <div className="error-banner">晨间队列读取失败：{String(query.error)}</div>}
    {runMorning.isError && <div className="error-banner">晨报任务启动失败：{String(runMorning.error)}</div>}
    <div className="review-layout morning-review-layout">
      <aside className="post-list">
        {items.map((item) => {
          const source = item.post || item.drafts[0]
          if (!source) return null
          const attention = item.drafts.filter((draft) => draft.status === 'needs_attention').length
          return <button key={item.postId} className={`post-list-item ${selectedPostId === item.postId ? 'active' : ''}`} onClick={() => setSelectedPostId(item.postId)}>
            <div className="post-list-top"><strong>{source.platform === 'Zhihu' ? source.handle : `@${source.handle}`} <span className={`badge ${source.platform === 'Zhihu' ? 'blue' : 'neutral'}`}>{source.platform === 'Zhihu' ? '知乎' : 'X'}</span></strong><span>{formatDate(source.posted_at)}</span></div>
            <p>{source.text}</p>
            <div className="post-list-meta"><span>{item.historical ? '历史修复' : item.drafts.length ? `${item.drafts.length} 只股票` : viewMeta[view].label}</span>{attention > 0 && <span>{attention} 条需核对</span>}</div>
          </button>
        })}
        {!items.length && <div className="empty-state compact">当前视图没有记录</div>}
      </aside>
      <div className="review-detail">
        {selected ? <PostDetail item={selected} reviewDate={reviewDate} onDone={refresh} /> : <div className="empty-state">选择左侧帖子查看详情</div>}
      </div>
    </div>
  </section>
}

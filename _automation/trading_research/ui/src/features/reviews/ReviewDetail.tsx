import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  AlertTriangle, Check, CheckCircle2, ExternalLink, FileText, Image as ImageIcon,
  Plus, RefreshCw, Sparkles, X, XCircle,
} from 'lucide-react'
import { api, formatDate } from '../../api'
import { useDirtyGuard } from '../../workspace'
import ActionToast from '../../components/ActionToast'
import type { DraftCorrectionType, Post, RecommendationDraft } from '../../types'

type MorningView = 'new' | 'processed' | 'pending' | 'failed' | 'approved'
type PendingFilter = 'all' | 'attention'

interface ReviewItem {
  postId: string
  post?: Post
  drafts: RecommendationDraft[]
  historical: boolean
}

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
  useDirtyGuard(fieldsChanged || Boolean(note))
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
  useDirtyGuard(Object.entries(form).some(([key, value]) => ["symbol", "security_name", "thesis", "evidence", "note"].includes(key) && Boolean(value)))
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

export default function PostDetail({ item, reviewDate, onDone }: { item: ReviewItem; reviewDate: string; onDone: () => void }) {
  const [showManual, setShowManual] = useState(false)
  const reprocess = useMutation({
    mutationFn: () => api.reprocessRecommendationPost(item.postId),
    onSuccess: onDone,
  })
  const source = item.post || item.drafts[0]
  if (!source) return <div className="empty-state">这条记录没有可展示的来源快照</div>
  const post = item.post
  const ocrText = source.ocr_text || item.drafts.find((draft) => draft.ocr_text)?.ocr_text || ''
  const sourceText = [source.text, source.article_text, source.quoted_text, ocrText, ...item.drafts.flatMap((draft) => [draft.text, draft.article_text, draft.quoted_text, draft.ocr_text])].filter(Boolean).join('\n')
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
    {source.local_media.length > 0 && <div className="media-grid">{source.local_media.map((media) => <a key={media.sha256} href={media.api_url} target="_blank" rel="noreferrer"><img src={media.api_url} alt="帖子图片快照" loading="lazy" /><span><ImageIcon size={14} />查看原图</span></a>)}</div>}
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


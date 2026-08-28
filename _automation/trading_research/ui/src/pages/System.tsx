import { type FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Container,
  Database,
  HardDrive,
  KeyRound,
  Play,
  RefreshCw,
  Server,
  Terminal,
  XCircle,
} from 'lucide-react'
import { api, formatDate } from '../api'
import type { FetchRun, XSessionSlot } from '../types'

type CredentialValues = { auth: string; ct0: string }

function runPresentation(status: FetchRun['status']) {
  if (status === 'running') return { label: 'Running', tone: 'running', icon: RefreshCw }
  if (status === 'success') return { label: 'Success', tone: 'ok', icon: CheckCircle2 }
  if (status === 'partial') return { label: 'Partial', tone: 'partial', icon: AlertTriangle }
  return { label: 'Failed', tone: 'bad', icon: XCircle }
}

export default function System() {
  const client = useQueryClient()
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const diagnostics = useQuery({
    queryKey: ['system-diagnostics'],
    queryFn: api.diagnostics,
    refetchInterval: 60_000,
    staleTime: 60_000,
  })
  const runs = useQuery({ queryKey: ['fetch-runs'], queryFn: api.fetchRuns, refetchInterval: 10_000 })
  const tasks = useQuery({ queryKey: ['operator-tasks'], queryFn: api.tasks, refetchInterval: 5_000 })
  const recoveryPreview = useQuery({ queryKey: ['collection-recovery-preview'], queryFn: api.collectionRecoveryPreview, refetchInterval: 30_000 })
  const [recoveryRunId, setRecoveryRunId] = useState('')
  const recoveryStatus = useQuery({
    queryKey: ['collection-recovery', recoveryRunId],
    queryFn: () => api.collectionRecoveryStatus(recoveryRunId),
    enabled: Boolean(recoveryRunId),
    refetchInterval: 5_000,
  })
  const [backup, setBackup] = useState<CredentialValues>({ auth: '', ct0: '' })
  const [sessionValues, setSessionValues] = useState<Record<number, CredentialValues>>({
    1: { auth: '', ct0: '' }, 2: { auth: '', ct0: '' }, 3: { auth: '', ct0: '' },
  })
  const [deepseekKey, setDeepseekKey] = useState('')
  const saveBackup = useMutation({
    mutationFn: () => api.saveNitterCredentials(backup.auth, backup.ct0),
    onSuccess: () => {
      setBackup({ auth: '', ct0: '' })
      client.invalidateQueries({ queryKey: ['health'] })
    },
  })
  const saveDeepSeek = useMutation({
    mutationFn: () => api.saveDeepSeekCredentials(deepseekKey),
    onSuccess: () => {
      setDeepseekKey('')
      client.invalidateQueries({ queryKey: ['health'] })
    },
  })
  const saveXSession = useMutation({
    mutationFn: (value: { slot: number; auth: string; ct0: string }) => api.saveXSession(value.slot, value.auth, value.ct0),
    onSuccess: (_value, variables) => {
      setSessionValues((current) => ({ ...current, [variables.slot]: { auth: '', ct0: '' } }))
      client.invalidateQueries({ queryKey: ['health'] })
    },
  })
  const verifyXSession = useMutation({
    mutationFn: (slot: number) => api.verifyXSession(slot),
    onSettled: () => client.invalidateQueries({ queryKey: ['health'] }),
  })
  const patchXSession = useMutation({
    mutationFn: (value: { slot: number; status: 'ready' | 'disabled' }) => api.patchXSession(value.slot, value.status),
    onSettled: () => client.invalidateQueries({ queryKey: ['health'] }),
  })
  const patchXPolicy = useMutation({
    mutationFn: (paused: boolean) => api.patchXPolicy({ paused, reason: paused ? 'manual console pause' : '' }),
    onSettled: () => client.invalidateQueries({ queryKey: ['health'] }),
  })
  const patchPublicBackup = useMutation({
    mutationFn: (value: { enabled?: boolean; paused?: boolean }) => api.patchPublicBackup({ ...value, reason: value.paused ? 'manual console pause' : '' }),
    onSettled: () => client.invalidateQueries({ queryKey: ['health'] }),
  })
  const startTask = useMutation({
    mutationFn: (task: 'morning' | 'fetch' | 'zhihu') => api.startTask(task),
    onSettled: () => {
      client.invalidateQueries({ queryKey: ['operator-tasks'] })
      client.invalidateQueries({ queryKey: ['fetch-runs'] })
      client.invalidateQueries({ queryKey: ['health'] })
      client.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
  })
  const startRecovery = useMutation({
    mutationFn: api.startCollectionRecovery,
    onSuccess: (value) => {
      setRecoveryRunId(value.run_id)
      client.invalidateQueries({ queryKey: ['collection-recovery-preview'] })
      client.invalidateQueries({ queryKey: ['fetch-runs'] })
    },
  })
  const h = health.data || diagnostics.data
    ? { ...(health.data || {}), ...(diagnostics.data || {}) } as typeof health.data
    : undefined
  const fetchRunning = tasks.data?.fetch?.status === 'running'
  const morningRunning = tasks.data?.morning?.status === 'running'
  const pipelineBusy = startTask.isPending || fetchRunning || morningRunning
  const error = health.error || saveXSession.error || verifyXSession.error || patchXSession.error || patchXPolicy.error || patchPublicBackup.error || saveBackup.error || saveDeepSeek.error || startTask.error || recoveryPreview.error || startRecovery.error
  const recoveryBusy = startRecovery.isPending || recoveryStatus.data?.status === 'running'

  return <section>
    <header className="page-header">
      <div><span className="eyebrow">DATA HEALTH</span><h1>数据健康</h1>{h?.recovery_mode === 'historical' && <span className="scope-note">行情历史只读 · 数据截至 {h.as_of || '未知'}</span>}</div>
      <div className="header-actions">
        <button className="primary-button" disabled={pipelineBusy} onClick={() => startTask.mutate('morning')}>
          {morningRunning ? <RefreshCw className="spin" size={16} /> : <Play size={16} />}
          {morningRunning ? '晨报运行中' : '运行完整晨报'}
        </button>
        <button className="secondary-button" disabled={pipelineBusy} onClick={() => startTask.mutate('fetch')}>
          <RefreshCw className={fetchRunning ? 'spin' : ''} size={16} />只补抓 X
        </button>
        <button className="secondary-button" disabled={pipelineBusy} onClick={() => startTask.mutate('zhihu')}>
          <RefreshCw size={16} />只补抓知乎
        </button>
      </div>
    </header>
    {error && <div className="error-banner">{String(error)}</div>}
    <div className="panel task-console">
      <div className="panel-heading"><div><h2>任务进度</h2><span>每 5 秒刷新，按钮只负责启动，不会阻塞页面</span></div></div>
      <TaskProgress
        label="晨报管线"
        stage={tasks.data?.morning?.stage || '尚未运行'}
        current={tasks.data?.morning?.progress_current || 0}
        total={tasks.data?.morning?.progress_total || 0}
        running={morningRunning}
      />
      <TaskProgress
        label="帖子采集"
        stage={tasks.data?.fetch?.stage || '尚未运行'}
        current={tasks.data?.fetch?.processed_kols || 0}
        total={tasks.data?.fetch?.total_kols || 0}
        running={fetchRunning}
      />
    </div>
      <div className="panel task-console">
      <div className="panel-heading"><div><h2>采集恢复</h2><span>先补最近 7 天；AI 503 不会阻塞帖子保存</span></div><button className="secondary-button" disabled={recoveryBusy || !recoveryPreview.data?.x_session_ready} onClick={() => startRecovery.mutate()}><RefreshCw className={recoveryBusy ? 'spin' : ''} size={16} />{recoveryBusy ? '恢复运行中' : '补齐最近 7 天'}</button></div>
      <div className="recovery-status-grid">
        {(recoveryPreview.data?.coverage || []).map((item) => <div key={item.platform}><span>{item.platform}</span><strong>{item.successful}/{item.target} 个账号</strong><small>覆盖率 {(item.coverage * 100).toFixed(0)}% · {item.window_start} 至 {item.window_end}</small></div>)}
      </div>
      {recoveryStatus.data && <div className="scope-note"><RefreshCw size={15} />恢复批次 {recoveryStatus.data.status} · 启动于 {formatDate(recoveryStatus.data.started_at)}{recoveryStatus.data.completed_at ? ` · 完成于 ${formatDate(recoveryStatus.data.completed_at)}` : ''}</div>}
      {!recoveryPreview.data?.x_session_ready && <div className="scope-note"><AlertTriangle size={15} />没有已验证的 X 会话；请先在下方完成至少一个槽位的保存和账号验证。</div>}
    </div>
    <div className="health-grid">
      <HealthItem icon={Terminal} label="Hermes gateway" ok={h?.component_status?.hermes_gateway.status === 'running'} value={`${h?.component_status?.hermes_gateway.status || 'unknown'}${h?.component_status?.hermes_gateway.pid ? ` / PID ${h.component_status.hermes_gateway.pid}` : ''}`} />
      <HealthItem icon={Terminal} label="KOL operator" ok={h?.component_status?.operator.status === 'available'} value={h?.component_status?.operator.status || 'unknown'} />
      <HealthItem icon={Terminal} label="X session pool" ok={h?.x_collection_status === 'ready'} value={`${h?.x_collection_status || 'pending_verification'} / ${h?.x_sessions?.global_remaining_24h ?? 0} global requests left`} />
      <HealthItem icon={Terminal} label="X public backup" ok={!!h?.public_backup?.enabled && !h?.public_backup?.paused_until} value={`${h?.public_backup?.enabled ? 'enabled' : 'disabled'} / ${h?.public_backup?.public_remaining_24h ?? 0} public requests left`} />
      <HealthItem icon={Database} label="Zhihu collection" ok={!!h?.zhihu_capture_available && h?.zhihu_fetch_status !== 'degraded'} value={`${h?.zhihu_active_kols || 0} active / ${h?.zhihu_paused_kols || 0} paused / ${h?.zhihu_fetch_status || 'never'}`} />
      <HealthItem icon={RefreshCw} label="Morning pipeline" ok={h?.morning_pipeline_task === 'installed'} value={`Zhihu 06:30、08:05、19:20 / X 07:20、19:00 / 08:45只审核 / ${h?.morning_pipeline_task || '-'}`} />
      <HealthItem icon={Terminal} label="Codex batch review" ok={!!h?.codex_cli} value={h?.codex_cli || 'Codex CLI not found'} />
      <HealthItem icon={RefreshCw} label="候选 AI 每日队列" ok={(h?.model_queue?.manual_attention ?? 0) === 0} value={`待模型 ${h?.model_queue?.processable_remaining ?? 0} / 待OCR ${h?.model_queue?.awaiting_ocr ?? 0} · 今日 ${h?.model_daily_budget?.attempted ?? 0}/${h?.model_daily_budget?.daily_limit ?? 250} · 预计 ${h?.model_queue?.estimated_days ?? 0} 天`} />
      <HealthItem icon={Terminal} label="OpenCode Go review AI" ok={!!h?.codex_cli} value={`${h?.deepseek_model || 'DeepSeek fallback'} / ${h?.deepseek_credentials_configured ? 'configured' : 'optional fallback unavailable'}`} />
      <HealthItem icon={Terminal} label="OCR RapidOCR" ok={!!h?.rapid_ocr_available} value={h?.rapid_ocr_available ? 'local OCR available' : 'run OCR installer'} />
      <HealthItem icon={Terminal} label="OCR experiment" ok value={h?.unlimited_ocr_available ? 'Unlimited-OCR available' : 'optional / not installed'} />
      <HealthItem icon={Database} label="Market warehouse" ok={!!h?.market?.ok} value={`${h?.market?.active_instruments || 0} active / ${h?.market?.coverage_count || 0} coverage`} />
      <HealthItem icon={Database} label="行情准入" ok={(h?.market_admissions?.failed ?? 0) === 0} value={`已发布 ${h?.market_admissions?.published ?? 0} / 待补 ${h?.market_admissions?.pending ?? 0} / 失败 ${h?.market_admissions?.failed ?? 0}`} />
      <HealthItem icon={Database} label="FreeStockDB" ok={!!h?.component_status?.freestockdb.ok} value={`${h?.component_status?.freestockdb.status || 'unknown'} / ${h?.freestockdb?.data_path || 'D盘数据未连接'}`} />
      <HealthItem icon={RefreshCw} label="Market and returns tasks" ok={h?.recovery_mode === 'historical' || (h?.market_sync_task === 'installed' && h?.return_task === 'installed')} value={h?.recovery_mode === 'historical' ? 'historical / intentionally disabled' : `market ${h?.market_sync_task || '-'} / returns ${h?.return_task || '-'}`} />
      <HealthItem icon={Container} label="Nitter fallback" ok={h?.fallback_mode === 'shadow' || (!!h?.nitter_ready && !!h?.redis_ready)} value={`${h?.fallback_mode || 'shadow'} / Nitter ${h?.nitter_ready ? 'ok' : 'shadow pending'} / Redis ${h?.redis_ready ? 'ok' : 'shadow pending'}`} />
      <HealthItem icon={Database} label="Posts database" ok={!!h?.database} value={h?.database || '-'} />
      <HealthItem icon={HardDrive} label="Media snapshots" ok value={`${((h?.media_bytes || 0) / 1024 / 1024).toFixed(1)} MB`} />
      <HealthItem icon={RefreshCw} label="Post-approval refresh" ok={h?.pipeline_refresh?.status !== 'failed'} value={`${h?.pipeline_refresh?.status || 'idle'} / ${h?.pipeline_refresh?.reason || h?.pipeline_refresh?.phase || '-'}`} />
    </div>
    <div className="content-grid system-grid">
      <div className="panel">
        <div className="panel-heading"><div><h2>晨间运行</h2><span>最近 5 次</span></div></div>
        <div className="run-list">
          {h?.morning_runs?.map((run) => <div key={run.run_id}>
            <span className={`status-icon ${run.status === 'completed' ? 'ok' : run.status === 'running' ? 'running' : 'bad'}`}><RefreshCw size={16} /></span>
            <div><strong>{run.review_date} / {run.status}</strong><span>{run.stage} · {run.progress_current}/{run.progress_total} · 处理 {run.reviewed_posts} / 草稿 {run.ready_drafts} / 异常 {run.attention_drafts} / 失败 {run.failed_posts}</span></div>
          </div>)}
          {!h?.morning_runs?.length && <div className="empty-state compact">暂无晨间运行记录</div>}
        </div>
      </div>
      <div className="panel">
        <div className="panel-heading"><div><h2>采集运行</h2><span>最近记录</span></div></div>
        <div className="run-list">
          {runs.data?.map((run) => {
            const state = runPresentation(run.status)
            const Icon = state.icon
            return <div key={run.run_id}>
              <span data-testid={`fetch-run-status-${run.run_id}`} className={`status-icon ${state.tone}`}>
                <Icon className={run.status === 'running' ? 'spin' : ''} size={16} />
              </span>
              <div><strong>{formatDate(run.started_at)} / {state.label}</strong><span>{run.stage} · 账号 {run.processed_kols}/{run.total_kols} · 新增 {run.new_posts} / 候选 {run.candidate_posts} / 失败 {run.failed_kols}</span></div>
            </div>
          })}
          {!runs.data?.length && <div className="empty-state compact">暂无采集运行记录</div>}
        </div>
      </div>
    </div>
    <div className="content-grid system-grid credential-stack-grid">
      {(h?.x_sessions?.slots || [1, 2, 3].map((slot_id) => ({ slot_id, label: `X session ${slot_id}`, status: 'pending_verification', credential_configured: false } as XSessionSlot))).map((slot) => {
        const values = sessionValues[slot.slot_id] || { auth: '', ct0: '' }
        const busy = saveXSession.isPending && saveXSession.variables?.slot === slot.slot_id
        const verifying = verifyXSession.isPending && verifyXSession.variables === slot.slot_id
        const ready = slot.status === 'ready'
        return <XSessionForm key={slot.slot_id} slot={slot} values={values} busy={busy || verifying} onChange={(next) => setSessionValues((current) => ({ ...current, [slot.slot_id]: next }))} onSave={() => saveXSession.mutate({ slot: slot.slot_id, auth: values.auth, ct0: values.ct0 })} onVerify={() => verifyXSession.mutate(slot.slot_id)} onToggle={() => patchXSession.mutate({ slot: slot.slot_id, status: ready ? 'disabled' : 'ready' })} />
      })}
      <CredentialForm title="Nitter backup session" configured={!!h?.nitter_credentials_configured} values={backup} busy={saveBackup.isPending} onChange={setBackup} onSubmit={(event) => { event.preventDefault(); saveBackup.mutate() }} />
      <form className="panel credential-form" onSubmit={(event) => { event.preventDefault(); saveDeepSeek.mutate() }}>
        <div className="panel-heading"><div><h2><KeyRound size={16} />OpenCode Go · DeepSeek V4 Flash</h2><span>Windows Credential Manager / {h?.deepseek_credentials_configured ? 'configured' : 'missing'}</span></div></div>
        <label>OpenCode Go API key<input type="password" autoComplete="off" value={deepseekKey} onChange={(event) => setDeepseekKey(event.target.value)} /></label>
        <button className="primary-button" disabled={saveDeepSeek.isPending || !deepseekKey}>安全保存</button>
      </form>
    </div>
    <div className="panel scope-note"><strong>X 采集保护</strong><span>三个会话共用滚动额度；轮换不会提高总额度。状态：{h?.x_sessions?.paused_until ? `全局暂停至 ${h.x_sessions.paused_until}` : '按批次轮换'}。</span><button className="secondary-button" onClick={() => patchXPolicy.mutate(!Boolean(h?.x_sessions?.paused_until))}>{h?.x_sessions?.paused_until ? '恢复自动采集' : '暂停 X 自动采集'}</button><span>公开单帖备用：{h?.public_backup?.paused_until ? `暂停至 ${h.public_backup.paused_until}` : h?.public_backup?.enabled ? '普通故障可用' : '已停用'}。</span><button className="secondary-button" onClick={() => patchPublicBackup.mutate(h?.public_backup?.enabled ? { paused: !Boolean(h.public_backup.paused_until) } : { enabled: true })}>{h?.public_backup?.enabled && !h.public_backup?.paused_until ? '暂停公开备用' : '启用公开备用'}</button></div>
  </section>
}

function TaskProgress({ label, stage, current, total, running }: { label: string; stage: string; current: number; total: number; running: boolean }) {
  const percent = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : stage === 'completed' ? 100 : running ? 4 : 0
  return <div className="task-progress-row">
    <div><strong>{label}</strong><span>{stage} · {total > 0 ? `${current}/${total}` : stage === 'completed' ? '运行已完成' : running ? '正在初始化' : '无运行中任务'}</span></div>
    <progress max={100} value={percent} aria-label={`${label} ${percent}%`} />
    <b>{percent}%</b>
  </div>
}

function CredentialForm({ title, configured, values, busy, onChange, onSubmit }: { title: string; configured: boolean; values: CredentialValues; busy: boolean; onChange: (value: CredentialValues) => void; onSubmit: (event: FormEvent) => void }) {
  return <form className="panel credential-form" onSubmit={onSubmit}>
    <div className="panel-heading"><div><h2><KeyRound size={16} />{title}</h2><span>Windows Credential Manager / {configured ? 'configured' : 'missing'}</span></div></div>
    <label>auth_token<input type="password" autoComplete="off" value={values.auth} onChange={(event) => onChange({ ...values, auth: event.target.value })} /></label>
    <label>ct0<input type="password" autoComplete="off" value={values.ct0} onChange={(event) => onChange({ ...values, ct0: event.target.value })} /></label>
    <button className="primary-button" disabled={busy || !values.auth || !values.ct0}>安全保存</button>
  </form>
}

function XSessionForm({ slot, values, busy, onChange, onSave, onVerify, onToggle }: { slot: XSessionSlot; values: CredentialValues; busy: boolean; onChange: (value: CredentialValues) => void; onSave: () => void; onVerify: () => void; onToggle: () => void }) {
  const status = slot.status === 'ready' ? 'ready' : slot.status === 'cooldown' ? 'cooldown' : slot.status === 'auth_required' ? 'auth_required' : slot.status === 'disabled' ? 'disabled' : 'pending_verification'
  return <form className="panel credential-form" onSubmit={(event) => { event.preventDefault(); onSave() }}>
    <div className="panel-heading"><div><h2><KeyRound size={16} />{slot.label || `X session ${slot.slot_id}`}</h2><span>Windows Credential Manager / {slot.credential_configured ? 'configured' : 'missing'} · {status}</span></div></div>
    <label>auth_token<input type="password" autoComplete="off" value={values.auth} onChange={(event) => onChange({ ...values, auth: event.target.value })} /></label>
    <label>ct0<input type="password" autoComplete="off" value={values.ct0} onChange={(event) => onChange({ ...values, ct0: event.target.value })} /></label>
    <div className="button-row"><button className="primary-button" disabled={busy || !values.auth || !values.ct0}>保存到槽位 {slot.slot_id}</button><button type="button" className="secondary-button" disabled={busy || !slot.credential_configured} onClick={onVerify}>验证账号</button>{slot.status !== 'pending_verification' && <button type="button" className="secondary-button" disabled={busy || (slot.status !== 'ready' && !slot.user_id)} onClick={onToggle}>{slot.status === 'ready' ? '停用' : '启用'}</button>}</div>
    <small>已用 {slot.used_24h ?? 0}/{90}（滚动 24 小时）{slot.user_id ? ` · X ID ${slot.user_id}` : ''}{slot.cooldown_until ? ` · 冷却至 ${slot.cooldown_until}` : ''}</small>
    {slot.duplicate_identity && <span className="scope-note">此槽位与其他槽位是同一 X 账号，共享限额，不会增加轮换额度。</span>}
    {slot.last_error && <span className="scope-note">最近状态：{slot.last_error_code || 'provider_error'} · {slot.last_error}</span>}
  </form>
}

function HealthItem({ icon: Icon, label, ok, value }: { icon: typeof Server; label: string; ok: boolean; value: string }) {
  return <div className="health-item"><Icon size={18} /><div><span>{label}</span><strong title={value}>{value}</strong></div><span className={`health-dot ${ok ? 'ok' : 'bad'}`} /></div>
}

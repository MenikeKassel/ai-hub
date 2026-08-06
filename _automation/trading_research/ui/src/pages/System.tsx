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
import type { FetchRun } from '../types'

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
  const runs = useQuery({ queryKey: ['fetch-runs'], queryFn: api.fetchRuns, refetchInterval: 10_000 })
  const tasks = useQuery({ queryKey: ['operator-tasks'], queryFn: api.tasks, refetchInterval: 5_000 })
  const [primary, setPrimary] = useState<CredentialValues>({ auth: '', ct0: '' })
  const [backup, setBackup] = useState<CredentialValues>({ auth: '', ct0: '' })
  const [deepseekKey, setDeepseekKey] = useState('')
  const savePrimary = useMutation({
    mutationFn: () => api.saveCredentials(primary.auth, primary.ct0),
    onSuccess: () => {
      setPrimary({ auth: '', ct0: '' })
      client.invalidateQueries({ queryKey: ['health'] })
    },
  })
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
  const startTask = useMutation({
    mutationFn: (task: 'morning' | 'fetch' | 'zhihu') => api.startTask(task),
    onSettled: () => {
      client.invalidateQueries({ queryKey: ['operator-tasks'] })
      client.invalidateQueries({ queryKey: ['fetch-runs'] })
      client.invalidateQueries({ queryKey: ['health'] })
      client.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
  })
  const h = health.data
  const fetchRunning = tasks.data?.fetch?.status === 'running'
  const morningRunning = tasks.data?.morning?.status === 'running'
  const pipelineBusy = startTask.isPending || fetchRunning || morningRunning
  const error = health.error || savePrimary.error || saveBackup.error || saveDeepSeek.error || startTask.error

  return <section>
    <header className="page-header">
      <div><span className="eyebrow">DATA HEALTH</span><h1>数据健康</h1></div>
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
    <div className="health-grid">
      <HealthItem icon={Terminal} label="Hermes gateway" ok={h?.component_status?.hermes_gateway.status === 'running'} value={`${h?.component_status?.hermes_gateway.status || 'unknown'}${h?.component_status?.hermes_gateway.pid ? ` / PID ${h.component_status.hermes_gateway.pid}` : ''}`} />
      <HealthItem icon={Terminal} label="KOL operator" ok={h?.component_status?.operator.status === 'available'} value={h?.component_status?.operator.status || 'unknown'} />
      <HealthItem icon={Terminal} label="X primary source" ok={!!h?.twitter_cli && !['failed', 'authentication_failed'].includes(h?.twitter_auth_status || '') && h?.component_status?.x.status !== 'rate_limited'} value={`${h?.twitter_cli ? 'twitter-cli available' : 'missing'} / ${h?.component_status?.x.status || h?.twitter_auth_status || 'unknown'}`} />
      <HealthItem icon={Database} label="Zhihu collection" ok={!!h?.zhihu_capture_available && h?.zhihu_fetch_status !== 'degraded'} value={`${h?.zhihu_active_kols || 0} active / ${h?.zhihu_paused_kols || 0} paused / ${h?.zhihu_fetch_status || 'never'}`} />
      <HealthItem icon={RefreshCw} label="Morning pipeline" ok={h?.morning_pipeline_task === 'installed'} value={`Zhihu 06:30 / X 07:20 08:05 08:45 / ${h?.morning_pipeline_task || '-'}`} />
      <HealthItem icon={Terminal} label="Codex batch review" ok={!!h?.codex_cli} value={h?.codex_cli || 'Codex CLI not found'} />
      <HealthItem icon={Terminal} label="OpenCode Go review AI" ok={!!h?.deepseek_credentials_configured} value={`${h?.deepseek_model || 'deepseek-v4-flash'} / ${h?.deepseek_credential_source || (h?.deepseek_credentials_configured ? 'configured' : 'missing key')}`} />
      <HealthItem icon={Terminal} label="OCR RapidOCR" ok={!!h?.rapid_ocr_available} value={h?.rapid_ocr_available ? 'local OCR available' : 'run OCR installer'} />
      <HealthItem icon={Terminal} label="OCR experiment" ok={!!h?.unlimited_ocr_available} value={h?.unlimited_ocr_available ? 'Unlimited-OCR available' : 'not installed'} />
      <HealthItem icon={Database} label="Market warehouse" ok={!!h?.market?.ok} value={`${h?.market?.active_instruments || 0} active / ${h?.market?.coverage_count || 0} coverage`} />
      <HealthItem icon={Database} label="FreeStockDB" ok={!!h?.component_status?.freestockdb.ok} value={`${h?.component_status?.freestockdb.status || 'unknown'} / third source and minute bars`} />
      <HealthItem icon={RefreshCw} label="Market and returns tasks" ok={h?.market_sync_task === 'installed' && h?.return_task === 'installed'} value={`market ${h?.market_sync_task || '-'} / returns ${h?.return_task || '-'}`} />
      <HealthItem icon={Container} label="Nitter fallback" ok={!!h?.nitter_ready && !!h?.redis_ready} value={`${h?.fallback_mode || 'shadow'} / Nitter ${h?.nitter_ready ? 'ok' : 'down'} / Redis ${h?.redis_ready ? 'ok' : 'down'}`} />
      <HealthItem icon={Database} label="Posts database" ok={!!h?.database} value={h?.database || '-'} />
      <HealthItem icon={HardDrive} label="Media snapshots" ok value={`${((h?.media_bytes || 0) / 1024 / 1024).toFixed(1)} MB`} />
      <HealthItem icon={RefreshCw} label="Post-approval refresh" ok={h?.pipeline_refresh?.status !== 'failed'} value={`${h?.pipeline_refresh?.status || 'idle'} / ${h?.pipeline_refresh?.phase || '-'}`} />
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
      <CredentialForm title="X primary session" configured={!!h?.twitter_credentials_configured} values={primary} busy={savePrimary.isPending} onChange={setPrimary} onSubmit={(event) => { event.preventDefault(); savePrimary.mutate() }} />
      <CredentialForm title="Nitter backup session" configured={!!h?.nitter_credentials_configured} values={backup} busy={saveBackup.isPending} onChange={setBackup} onSubmit={(event) => { event.preventDefault(); saveBackup.mutate() }} />
      <form className="panel credential-form" onSubmit={(event) => { event.preventDefault(); saveDeepSeek.mutate() }}>
        <div className="panel-heading"><div><h2><KeyRound size={16} />OpenCode Go · DeepSeek V4 Flash</h2><span>{h?.deepseek_credential_source === 'opencode-config' ? 'Reusing local OpenCode config' : `Windows Credential Manager / ${h?.deepseek_credentials_configured ? 'configured' : 'missing'}`}</span></div></div>
        <label>OpenCode Go API key<input type="password" autoComplete="off" value={deepseekKey} onChange={(event) => setDeepseekKey(event.target.value)} /></label>
        <button className="primary-button" disabled={saveDeepSeek.isPending || !deepseekKey}>安全保存</button>
      </form>
    </div>
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

function HealthItem({ icon: Icon, label, ok, value }: { icon: typeof Server; label: string; ok: boolean; value: string }) {
  return <div className="health-item"><Icon size={18} /><div><span>{label}</span><strong title={value}>{value}</strong></div><span className={`health-dot ${ok ? 'ok' : 'bad'}`} /></div>
}

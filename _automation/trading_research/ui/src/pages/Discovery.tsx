import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Compass, RefreshCw, Search, UserPlus } from 'lucide-react'
import { useState } from 'react'

type Platform = {
  platform: string
  display_name: string
  available: boolean
  provider: string
  capabilities: Record<string, boolean>
  health: Record<string, unknown>
}

const platformStatusLabel: Record<string, string> = {
  ready: '可用',
  degraded: '降级',
  needs_login: '需要登录',
  blocked: '已阻断',
  manual_only: '仅手工导入',
  untested: '未验收',
}

type Candidate = {
  candidate_id: string
  platform: string
  handle: string
  display_name: string
  profile_url: string
  bio: string
  state: string
  ai_score?: number | null
  score_json?: { reasons?: string[]; total?: number }
  last_seen_at: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail || response.statusText)
  }
  return response.json() as Promise<T>
}

export default function Discovery() {
  const queryClient = useQueryClient()
  const [platform, setPlatform] = useState('x')
  const [query, setQuery] = useState('')
  const [candidateState, setCandidateState] = useState('new')
  const [notice, setNotice] = useState('')
  const platforms = useQuery({
    queryKey: ['discovery-platforms'],
    queryFn: () => request<Platform[]>('/api/discovery/platforms'),
  })
  const candidates = useQuery({
    queryKey: ['discovery-candidates', candidateState],
    queryFn: () => request<Candidate[]>(`/api/discovery/candidates?state=${candidateState}&limit=100`),
  })
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['discovery-platforms'] })
    void queryClient.invalidateQueries({ queryKey: ['discovery-candidates'] })
  }
  const run = useMutation({
    mutationFn: () => request('/api/discovery/runs', {
      method: 'POST',
      body: JSON.stringify({ platform, query, limit: 50 }),
    }),
    onSuccess: () => { setNotice('发现任务已完成'); refresh() },
    onError: (error) => setNotice(error.message),
  })
  const action = useMutation({
    mutationFn: ({ id, verb, note }: { id: string; verb: string; note?: string }) => request(`/api/discovery/candidates/${id}/${verb}`, {
      method: 'POST',
      body: JSON.stringify({ note: note || '' }),
    }),
    onSuccess: () => { setNotice('候选状态已更新'); refresh() },
    onError: (error) => setNotice(error.message),
  })
  const selectedPlatform = (platforms.data || []).find((item) => item.platform === platform)
  const selectedStatus = String(selectedPlatform?.health.status || (selectedPlatform?.available ? 'ready' : 'untested'))
  const canDiscover = Boolean(selectedPlatform?.available && ['ready', 'degraded'].includes(selectedStatus))

  return <>
    <header className="page-header">
      <div><span className="eyebrow">ACCOUNT DISCOVERY</span><h1>全平台发现</h1><p className="page-subtitle">先发现候选账号，再人工收纳；候选评分不会自动进入KOL监控或收益审计。</p></div>
      <button className="icon-button" title="刷新" onClick={refresh}><RefreshCw size={16} /></button>
    </header>
    {notice && <div className="notice-banner">{notice}</div>}
    <div className="discovery-layout">
      <section className="panel">
        <div className="panel-heading"><div><h2>启动发现</h2><span>使用已配置的平台适配器</span></div><Compass size={18} /></div>
        <div className="form-grid">
          <label>平台<select value={platform} onChange={(event) => setPlatform(event.target.value)}>{(platforms.data || []).map((item) => { const status = String(item.health.status || (item.available ? 'ready' : 'untested')); return <option key={item.platform} value={item.platform}>{item.display_name} · {platformStatusLabel[status] || status}</option> })}</select></label>
          <label className="wide">关键词或主页<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="账号、主页或关键词" /></label>
        </div>
        <button className="primary-button" disabled={!query.trim() || run.isPending || !canDiscover} onClick={() => run.mutate()}><Search size={16} />开始发现</button>
        {!canDiscover && selectedPlatform && <div className="notice-banner">当前平台不可启动：{String(selectedPlatform.health.reason || platformStatusLabel[selectedStatus] || selectedStatus)}</div>}
        <div className="discovery-platform-list">{(platforms.data || []).map((item) => { const status = String(item.health.status || (item.available ? 'ready' : 'untested')); return <div className="discovery-platform" key={item.platform}><strong>{item.display_name}</strong><span className={`badge ${item.available ? 'positive' : 'neutral'}`}>{platformStatusLabel[status] || status}</span><small>{String(item.health.reason || item.provider || '无适配器')}</small></div> })}</div>
      </section>
      <section className="panel discovery-queue">
        <div className="panel-heading"><div><h2>候选账号</h2><span>接受后才会创建正式KOL身份</span></div><UserPlus size={18} /></div>
        <div className="segmented"><button className={candidateState === 'new' ? 'active' : ''} onClick={() => setCandidateState('new')}>待处理</button><button className={candidateState === 'reviewing' ? 'active' : ''} onClick={() => setCandidateState('reviewing')}>复核中</button><button className={candidateState === 'accepted' ? 'active' : ''} onClick={() => setCandidateState('accepted')}>已收纳</button><button className={candidateState === 'rejected' ? 'active' : ''} onClick={() => setCandidateState('rejected')}>已排除</button></div>
        {candidates.isLoading ? <div className="loading">加载中...</div> : <div className="table-scroll"><table><thead><tr><th>平台</th><th>账号</th><th>简介</th><th>AI分数</th><th>状态</th><th>操作</th></tr></thead><tbody>{(candidates.data || []).map((item) => <tr key={item.candidate_id}><td>{item.platform}</td><td><strong>{item.display_name}</strong><span className="secondary-line">@{item.handle}</span></td><td className="discovery-bio">{item.bio || '无简介'}</td><td>{item.ai_score ?? '-'}</td><td><span className="badge neutral">{item.state}</span></td><td className="actions">{item.state === 'new' && <><button className="icon-button" title="AI评分" onClick={() => action.mutate({ id: item.candidate_id, verb: 'score' })}><Compass size={15} /></button><button className="icon-button" title="收纳" onClick={() => action.mutate({ id: item.candidate_id, verb: 'accept' })}><UserPlus size={15} /></button></>}{item.state !== 'accepted' && item.state !== 'rejected' && <button className="text-button" onClick={() => action.mutate({ id: item.candidate_id, verb: 'reject', note: '人工排除' })}>排除</button>}</td></tr>)}</tbody></table></div>}
        {!candidates.isLoading && !(candidates.data || []).length && <div className="empty-state">当前队列为空</div>}
      </section>
    </div>
  </>
}

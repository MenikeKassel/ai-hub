import { FormEvent, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, Check, ExternalLink, History, Pencil, Pause, Play, Plus, RefreshCw, UserRound, X } from 'lucide-react'
import { api, formatDate, percent } from '../api'
import type { Kol } from '../types'

export default function Kols() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['kols'], queryFn: api.kols })
  const leaderboard = useQuery({ queryKey: ['kol-leaderboard'], queryFn: api.kolLeaderboard })
  const digestAuthors = useQuery({ queryKey: ['digest-authors'], queryFn: api.digestAuthors })
  const [showAdd, setShowAdd] = useState(false)
  const [view, setView] = useState<'accounts' | 'attributions' | 'performance'>('accounts')
  const [platformFilter, setPlatformFilter] = useState<'all' | 'X' | 'Zhihu'>('all')
  const [form, setForm] = useState<{ display_name: string; handle: string; domain: string; platform: 'X' | 'Zhihu' }>({ display_name: '', handle: '', domain: '', platform: 'X' })
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editForm, setEditForm] = useState({ display_name: '', domain: '' })
  const create = useMutation({ mutationFn: () => api.createKol(form), onSuccess: () => { client.invalidateQueries({ queryKey: ['kols'] }); setShowAdd(false); setForm({ display_name: '', handle: '', domain: '', platform: 'X' }) } })
  const patch = useMutation({ mutationFn: ({ id, value }: { id: number; value: Partial<Kol> }) => api.patchKol(id, value), onSuccess: () => { client.invalidateQueries({ queryKey: ['kols'] }); setEditingId(null) } })
  const backfill = useMutation({ mutationFn: (id: number) => api.queueKolBackfill(id), onSuccess: () => client.invalidateQueries({ queryKey: ['kols'] }) })
  const submit = (event: FormEvent) => { event.preventDefault(); create.mutate() }
  const beginEdit = (kol: Kol) => { setEditingId(kol.id); setEditForm({ display_name: kol.display_name, domain: kol.domain }) }
  const resolvedAuthors = digestAuthors.data?.filter((author) => author.profile_url).length || 0
  const accountRows = query.data?.filter((kol) => platformFilter === 'all' || kol.platform === platformFilter) || []

  return <section>
    <header className="page-header"><div><span className="eyebrow">WATCHED ACCOUNTS</span><h1>KOL管理</h1></div><button className="primary-button" onClick={() => setShowAdd(!showAdd)}><Plus size={16} />新增账号</button></header>
    {showAdd && <form className="inline-form" onSubmit={submit}>
      <label>显示名称<input required value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
      <label>平台<select value={form.platform} onChange={(event) => setForm({ ...form, platform: event.target.value as 'X' | 'Zhihu' })}><option value="X">X</option><option value="Zhihu">知乎</option></select></label>
      <label>{form.platform === 'X' ? 'X用户名' : '知乎 url_token'}<input required placeholder={form.platform === 'X' ? '不含 @' : '主页 /people/ 后的标识'} value={form.handle} onChange={(event) => setForm({ ...form, handle: event.target.value })} /></label>
      <label>研究领域<input value={form.domain} onChange={(event) => setForm({ ...form, domain: event.target.value })} /></label>
      <button className="primary-button" disabled={create.isPending}>保存</button>
    </form>}
    {(create.error || patch.error || backfill.error) && <div className="error-banner">{String(create.error || patch.error || backfill.error)}</div>}
    <div className="segmented kols-view-switch" aria-label="KOL管理视图">
      <button className={view === 'accounts' ? 'active' : ''} onClick={() => setView('accounts')}>采集账号</button>
      <button className={view === 'attributions' ? 'active' : ''} onClick={() => setView('attributions')}>知乎署名来源 <span>{digestAuthors.data?.length || 0}</span></button>
      <button className={view === 'performance' ? 'active' : ''} onClick={() => setView('performance')}>阶段表现</button>
    </div>
    {view === 'performance' && <div className="panel kol-leaderboard">
      <div className="panel-heading"><div><h2>KOL阶段表现</h2><span>只统计已冻结、已验证且可执行的推荐事件</span></div><span className="badge neutral">按超额收益审计</span></div>
      <div className="scope-note"><BarChart3 size={15} />阶段表现已升级为独立工作台，支持平台分组、批次等权、近期窗口和趋势查看。<button className="text-button" onClick={() => { window.location.hash = '/performance' }}>打开 KOL 表现</button></div>
      <div className="table-scroll"><table><thead><tr><th>排名</th><th>KOL</th><th>阶段</th><th>可执行事件</th><th>1W</th><th>1M</th><th>3M</th><th>6M</th></tr></thead>
      <tbody>{leaderboard.data?.rows.map((row) => <tr key={row.kol_name}>
        <td className="mono">{row.rank || '-'}</td><td><strong>{row.kol_name}</strong>{row.score !== null && <span className="secondary-line">观察分 {row.score.toFixed(2)}</span>}</td>
        <td><span className={`badge ${row.tier === 'reliable' || row.tier === 'long_term' ? 'green' : row.tier === 'provisional' || row.tier === 'watch' ? 'amber' : 'neutral'}`}>{({ collecting: '样本中', watch: '观察中', provisional: '初步排名', reliable: '较可信', long_term: '长期验证' } as const)[row.tier]}</span></td>
        <td>{row.executable_event_count} / {row.event_count}</td>
        {(['1W', '1M', '3M', '6M'] as const).map((horizon) => { const value = row.horizons[horizon]; return <td key={horizon}><strong>{percent(value.median_excess)}</strong><span className="secondary-line">{value.samples} 条 · 胜率 {percent(value.win_rate)}</span></td> })}
      </tr>)}</tbody></table></div>
      {!leaderboard.isLoading && !leaderboard.data?.rows.length && <div className="empty-state compact">尚无可统计KOL</div>}
    </div>}
    {view === 'accounts' && <div className="panel">
      <div className="panel-heading"><div><h2>Accounts</h2><span>{accountRows.length} / {query.data?.length || 0}</span></div><div className="segmented" aria-label="platform filter"><button className={platformFilter === 'all' ? 'active' : ''} onClick={() => setPlatformFilter('all')}>All</button><button className={platformFilter === 'X' ? 'active' : ''} onClick={() => setPlatformFilter('X')}>X</button><button className={platformFilter === 'Zhihu' ? 'active' : ''} onClick={() => setPlatformFilter('Zhihu')}>Zhihu</button></div></div>
      <div className="table-scroll"><table><thead><tr><th>KOL</th><th>领域</th><th>最近成功</th><th>最后帖子ID</th><th>采集状态</th><th>补抓状态</th><th>账号状态</th><th aria-label="操作" /></tr></thead>
      <tbody>{accountRows.map((kol) => <tr key={kol.id}>
        <td>{editingId === kol.id ? <input className="table-input" aria-label="编辑显示名称" value={editForm.display_name} onChange={(event) => setEditForm({ ...editForm, display_name: event.target.value })} /> : <><strong>{kol.display_name}</strong> <span className={`badge ${kol.platform === 'Zhihu' ? 'blue' : 'neutral'}`}>{kol.platform === 'Zhihu' ? '知乎' : 'X'}</span><a className="secondary-line" href={kol.profile_url} target="_blank" rel="noreferrer">{kol.platform === 'X' ? '@' : ''}{kol.handle}</a></>}</td>
        <td>{editingId === kol.id ? <input className="table-input" aria-label="编辑研究领域" value={editForm.domain} onChange={(event) => setEditForm({ ...editForm, domain: event.target.value })} /> : kol.domain || '-'}</td>
        <td>{formatDate(kol.last_success_at || kol.last_fetched_at)}</td><td className="mono">{kol.last_post_id || '-'}</td>
        <td><span className={`badge ${kol.fetch_status === 'success' ? 'green' : kol.fetch_status === 'failed' || kol.fetch_status === 'gap_detected' ? 'red' : 'neutral'}`}>{kol.fetch_status}</span>{kol.last_gap_at && <span className="secondary-line">最近缺口 {formatDate(kol.last_gap_at)}</span>}{kol.consecutive_failures > 0 && <span className="secondary-line">连续失败 {kol.consecutive_failures} 次</span>}</td>
        <td><span className={`badge ${kol.backfill_status === 'queued' || kol.backfill_status === 'needs_review' ? 'amber' : 'neutral'}`}>{kol.backfill_status === 'queued' ? `已补 ${kol.backfill_completed_depth}/${kol.backfill_requested}` : kol.backfill_status === 'needs_review' ? `需复核 ${kol.backfill_result_count}/${kol.backfill_requested}` : kol.backfill_status}</span>{kol.backfill_warning && <span className="secondary-line" title={kol.backfill_warning}>返回数量不足，点击可重试</span>}</td>
        <td><span className={`badge ${kol.status === 'active' ? 'green' : 'neutral'}`}>{kol.status === 'active' ? '启用' : '暂停'}</span></td>
        <td className="actions">{editingId === kol.id ? <><button className="icon-button" title="保存修改" onClick={() => patch.mutate({ id: kol.id, value: editForm })}><Check size={16} /></button><button className="icon-button" title="取消修改" onClick={() => setEditingId(null)}><X size={16} /></button></> : <><button className="icon-button" title="补抓最近200条" disabled={backfill.isPending || kol.backfill_status === 'queued'} onClick={() => backfill.mutate(kol.id)}><History size={16} /></button><button className="icon-button" title="编辑KOL" onClick={() => beginEdit(kol)}><Pencil size={16} /></button><button className="icon-button" title={kol.status === 'active' ? '暂停采集' : '恢复采集'} onClick={() => patch.mutate({ id: kol.id, value: { status: kol.status === 'active' ? 'paused' : 'active' } })}>{kol.status === 'active' ? <Pause size={16} /> : <Play size={16} />}</button></>}</td>
      </tr>)}</tbody></table></div>
      {query.isLoading && <div className="loading"><RefreshCw className="spin" size={18} />加载账号</div>}
    </div>}
    {view === 'attributions' && <div className="panel digest-authors-panel">
      <div className="panel-heading"><div><h2>知乎聚合署名来源</h2><span>来自 NEVEN 日更摘要；只作二手线索，不直接计入推荐收益</span></div><span className="badge blue">已定位 {resolvedAuthors} / {digestAuthors.data?.length || 0}</span></div>
      <div className="table-scroll"><table><thead><tr><th>博主</th><th>最近摘要</th><th>提及次数</th><th>股票代码</th><th aria-label="来源" /></tr></thead>
      <tbody>{digestAuthors.data?.slice(0, 50).map((author) => <tr key={author.author_name}>
        <td>{author.profile_url ? <a href={author.profile_url} target="_blank" rel="noreferrer"><strong>{author.author_name}</strong></a> : <strong>{author.author_name}</strong>}<span className="secondary-line">{author.profile_url ? '已定位原始主页' : '待补原始主页'}</span></td>
        <td><strong>{author.latest_title || '-'}</strong><span className="secondary-line digest-summary">{author.latest_summary.slice(0, 180) || '-'}</span></td>
        <td>{author.summary_count}</td><td className="mono">{author.symbols.join(', ') || '-'}</td>
        <td className="actions">{author.profile_url && <a className="icon-button" title="打开原作者主页" href={author.profile_url} target="_blank" rel="noreferrer"><UserRound size={16} /></a>}<a className="icon-button" title="打开聚合摘要" href={author.latest_source_url} target="_blank" rel="noreferrer"><ExternalLink size={16} /></a></td>
      </tr>)}</tbody></table></div>
      {!digestAuthors.isLoading && !digestAuthors.data?.length && <div className="empty-state compact">完成首次知乎采集后显示署名博主</div>}
    </div>}
  </section>
}

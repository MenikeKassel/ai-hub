import { lazy, Suspense, type ComponentType } from 'react'
import { Archive, Database, ListChecks, Radio, Rows3, Users } from 'lucide-react'
import PipelineStatusBar from './components/PipelineStatusBar'
import { navigate, updateRoute, useRoute, type Workspace } from './workspace'

type LazyModule = { default: ComponentType<any> }

function lazyWithRecovery(loader: () => Promise<LazyModule>, key: string) {
  return lazy(async () => {
    try {
      const loaded = await loader()
      sessionStorage.removeItem(`kol-chunk-reload:${key}`)
      return loaded
    } catch (error) {
      const marker = `kol-chunk-reload:${key}`
      const lastReload = Number(sessionStorage.getItem(marker) || 0)
      if (Date.now() - lastReload > 60_000) {
        sessionStorage.setItem(marker, String(Date.now()))
        window.location.reload()
        return new Promise<LazyModule>(() => undefined)
      }
      throw error
    }
  })
}

const ReviewQueue = lazyWithRecovery(() => import('./pages/ReviewQueue'), 'reviews')
const EventManagement = lazyWithRecovery(() => import('./pages/EventManagement'), 'events')
const Backtests = lazyWithRecovery(() => import('./pages/Backtests'), 'returns')
const Kols = lazyWithRecovery(() => import('./pages/Kols'), 'kols')
const KolPerformance = lazyWithRecovery(() => import('./pages/KolPerformance'), 'performance')
const System = lazyWithRecovery(() => import('./pages/System'), 'system')
const StockLeads = lazyWithRecovery(() => import('./pages/StockLeads'), 'history')
const Discovery = lazyWithRecovery(() => import('./pages/Discovery'), 'discovery')

const workspaces: Array<{ key: Workspace; label: string; detail: string; icon: typeof Radio }> = [
  { key: 'reviews', label: '审核工作台', detail: '原文 → 核对 → 确认', icon: ListChecks },
  { key: 'events', label: '事件研究', detail: '证据、行情与收益', icon: Rows3 },
  { key: 'kols', label: 'KOL 库', detail: '账号、表现与发现', icon: Users },
  { key: 'history', label: '资料归档', detail: '历史资料与股票提及', icon: Archive },
  { key: 'system', label: '运行中心', detail: '任务、健康与连接', icon: Database },
]
const tabs: Partial<Record<Workspace, Array<[string, string]>>> = {
  events: [['records', '正式事件'], ['returns', '收益审计']],
  kols: [['accounts', '监控账号'], ['performance', 'KOL 表现'], ['discovery', '账号发现']],
  system: [['tasks', '任务与队列'], ['health', '数据健康'], ['connections', '连接与凭据']],
}

export default function App() {
  const route = useRoute()
  const page = route.workspace
  const options = tabs[page] || []
  const selectedTab = options.some(([key]) => key === route.params.get('tab')) ? route.params.get('tab')! : options[0]?.[0]
  return <div className="app-shell workbench-v4">
    <aside className="sidebar">
      <div className="brand-block"><div className="brand-mark"><Radio size={20} /></div><div><strong>KOL 研究台</strong><span>从观点到可核对的证据</span></div></div>
      <span className="nav-caption">研究工作区</span>
      <nav aria-label="主导航">{workspaces.map(({ key, label, detail, icon: Icon }) => <button key={key} aria-label={`${key === 'reviews' ? '今日审核' : key === 'events' ? '正式事件 收益审计' : key === 'kols' ? 'KOL管理' : key === 'history' ? '历史归档' : '数据健康'} ${label}`} aria-current={page === key ? 'page' : undefined} className={page === key ? 'nav-item active' : 'nav-item'} onClick={() => navigate(key)}><Icon size={19} /><span>{label}<small>{detail}</small></span></button>)}</nav>
      <div className="sidebar-footer"><span className="status-dot" />本地研究环境 <span className="version-tag">V4</span></div>
    </aside>
    <div className="mobile-nav" aria-label="移动导航">{workspaces.map(({ key, label, icon: Icon }) => <button key={key} aria-current={page === key ? 'page' : undefined} aria-label={label} className={page === key ? 'active' : ''} onClick={() => navigate(key)}><Icon size={18} /><span>{label}</span></button>)}</div>
    <main className="main-content">
      <PipelineStatusBar />
      {options.length > 0 && <nav className="workspace-tabs" aria-label="工作区页签">{options.map(([key, label]) => <button key={key} aria-current={selectedTab === key ? 'page' : undefined} className={selectedTab === key ? 'active' : ''} onClick={() => updateRoute({ tab: key })}>{label}</button>)}</nav>}
      <Suspense fallback={<div className="loading" role="status">加载工作区…</div>}>
        {page === 'reviews' && <ReviewQueue />}
        {page === 'events' && (selectedTab === 'returns' ? <Backtests /> : <EventManagement />)}
        {page === 'kols' && (selectedTab === 'performance' ? <KolPerformance /> : selectedTab === 'discovery' ? <Discovery /> : <Kols />)}
        {page === 'system' && <System section={selectedTab as 'tasks' | 'health' | 'connections'} />}
        {page === 'history' && <StockLeads />}
      </Suspense>
    </main>
  </div>
}

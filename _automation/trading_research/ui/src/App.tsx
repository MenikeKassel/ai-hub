import { lazy, Suspense, useEffect, useState } from 'react'
import { Activity, Archive, BarChart3, Database, Layers3, ListChecks, Radio, Rows3, Users } from 'lucide-react'
import PipelineStatusBar from './components/PipelineStatusBar'

const ReviewQueue = lazy(() => import('./pages/ReviewQueue'))
const EventManagement = lazy(() => import('./pages/EventManagement'))
const Backtests = lazy(() => import('./pages/Backtests'))
const Kols = lazy(() => import('./pages/Kols'))
const KolPerformance = lazy(() => import('./pages/KolPerformance'))
const System = lazy(() => import('./pages/System'))
const StockLeads = lazy(() => import('./pages/StockLeads'))
const BoardMainline = lazy(() => import('./pages/BoardMainline'))

type PageKey = 'reviews' | 'events' | 'backtests' | 'boards' | 'performance' | 'kols' | 'system' | 'history'

const pages: Array<{ key: PageKey; label: string; shortLabel: string; icon: typeof Activity }> = [
  { key: 'reviews', label: '今日审核', shortLabel: '审核', icon: ListChecks },
  { key: 'events', label: '正式事件', shortLabel: '事件', icon: Rows3 },
  { key: 'backtests', label: '收益审计', shortLabel: '收益', icon: Activity },
  { key: 'boards', label: '板块主线', shortLabel: '板块', icon: Layers3 },
  { key: 'performance', label: 'KOL表现', shortLabel: '表现', icon: BarChart3 },
  { key: 'kols', label: 'KOL管理', shortLabel: 'KOL', icon: Users },
  { key: 'system', label: '数据健康', shortLabel: '健康', icon: Database },
  { key: 'history', label: '历史归档', shortLabel: '历史', icon: Archive },
]

function pageFromHash(): PageKey {
  const value = window.location.hash.replace('#/', '').split('?')[0]
  const legacy: Record<string, PageKey> = { overview: 'reviews', leads: 'history', market: 'system' }
  const normalized = legacy[value] || value
  return pages.some((page) => page.key === normalized) ? normalized as PageKey : 'reviews'
}

export default function App() {
  const [page, setPage] = useState<PageKey>(pageFromHash)
  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const navigate = (key: PageKey) => { window.location.hash = `/${key}`; setPage(key) }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-block"><div className="brand-mark"><Radio size={18} /></div><div><strong>KOL研究台</strong><span>证据与收益审计</span></div></div>
      <nav aria-label="主导航">{pages.map(({ key, label, icon: Icon }) => <button key={key} className={page === key ? 'nav-item active' : 'nav-item'} onClick={() => navigate(key)}><Icon size={18} /><span>{label}</span></button>)}</nav>
      <div className="sidebar-footer"><span className="status-dot" />本地研究环境</div>
    </aside>
    <div className="mobile-nav" aria-label="移动导航">{pages.map(({ key, label, shortLabel, icon: Icon }) => <button key={key} className={page === key ? 'active' : ''} title={label} onClick={() => navigate(key)}><Icon size={18} /><span>{shortLabel}</span></button>)}</div>
    <main className="main-content"><PipelineStatusBar /><Suspense fallback={<div className="loading">加载工作台…</div>}>
      {page === 'reviews' && <ReviewQueue />}
      {page === 'events' && <EventManagement />}
      {page === 'backtests' && <Backtests />}
      {page === 'boards' && <BoardMainline />}
      {page === 'performance' && <KolPerformance />}
      {page === 'kols' && <Kols />}
      {page === 'system' && <System />}
      {page === 'history' && <StockLeads />}
    </Suspense></main>
  </div>
}

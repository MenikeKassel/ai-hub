import { useEffect, useState, useSyncExternalStore } from 'react'

export type Workspace = 'reviews' | 'events' | 'kols' | 'history' | 'system'
const dirtyEditors = new Set<symbol>()
let acceptedHash = window.location.hash
let permittedHash = ''

export function parseRoute(hash: string) {
  const [path, search = ''] = hash.replace(/^#\/?/, '').split('?')
  const params = new URLSearchParams(search)
  const aliases: Record<string, [Workspace, string?]> = {
    overview: ['reviews'], backtests: ['events', 'returns'], performance: ['kols', 'performance'],
    discovery: ['kols', 'discovery'], leads: ['history'], market: ['system', 'health'],
  }
  const alias = aliases[path]
  const workspace = alias?.[0] || (['reviews', 'events', 'kols', 'history', 'system'].includes(path) ? path as Workspace : 'reviews')
  if (alias?.[1]) params.set('tab', alias[1])
  return { workspace, params }
}

function canLeave() {
  return dirtyEditors.size === 0 || window.confirm('有尚未保存的修改。放弃这些修改并切换？')
}

window.addEventListener('hashchange', () => {
  const next = window.location.hash
  if (next !== permittedHash && next !== acceptedHash && !canLeave()) {
    window.history.replaceState(null, '', acceptedHash || '#/reviews')
  } else acceptedHash = next
  permittedHash = ''
  window.dispatchEvent(new Event('kol-route-change'))
})
window.addEventListener('beforeunload', (event) => {
  if (dirtyEditors.size) { event.preventDefault(); event.returnValue = '' }
})

export function navigate(workspace: Workspace, values: Record<string, string> = {}) {
  const params = new URLSearchParams(values)
  const hash = `#/${workspace}${params.size ? `?${params}` : ''}`
  if (hash === window.location.hash || !canLeave()) return
  permittedHash = hash
  window.location.hash = hash
}

export function updateRoute(values: Record<string, string | number | null>, replace = false) {
  const route = parseRoute(window.location.hash)
  Object.entries(values).forEach(([key, value]) => value === null || value === '' ? route.params.delete(key) : route.params.set(key, String(value)))
  if (replace) {
    const hash = `#/${route.workspace}?${route.params}`
    window.history.replaceState(null, '', hash)
    acceptedHash = hash
    window.dispatchEvent(new Event('kol-route-change'))
  } else navigate(route.workspace, Object.fromEntries(route.params))
}

const subscribe = (callback: () => void) => {
  window.addEventListener('kol-route-change', callback)
  return () => window.removeEventListener('kol-route-change', callback)
}
export function useRoute() {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  return parseRoute(hash)
}

export function useDirtyGuard(dirty: boolean) {
  const [key] = useState(() => Symbol('editor'))
  useEffect(() => {
    if (dirty) dirtyEditors.add(key)
    else dirtyEditors.delete(key)
    return () => { dirtyEditors.delete(key) }
  }, [dirty, key])
}

export const shanghaiDate = (offset = 0) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date(Date.now() + offset * 86400000))

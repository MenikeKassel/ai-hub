import type { Query } from '@tanstack/react-query'

export function taskInterval(query: Query): number {
  if (document.hidden) return 30_000
  const value = query.state.data as Record<string, { status?: string }> | undefined
  return value && Object.values(value).some((item) => item && typeof item === 'object' && item.status === 'running') ? 5_000 : 30_000
}

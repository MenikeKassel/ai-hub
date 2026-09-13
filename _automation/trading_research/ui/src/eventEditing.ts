import type { Event, EventAmendment, EventAmendmentResult } from './types'

export function toLocalDateTimeInput(value: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return ''
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, -1)
}

export function toEventTimestamp(value: string, original: string): string {
  if (value === toLocalDateTimeInput(original)) return original
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : ''
}

export function eventForm(event: Event) {
  return {
    kol_name: event.kol_name, platform: event.platform || 'X', source_url: event.source_url,
    source_note: event.source_note, posted_at: toLocalDateTimeInput(event.posted_at),
    symbol: event.symbol, security_name: event.security_name,
    direction: event.direction || 'long', thesis: event.thesis,
  }
}

export function eventChanges(event: Event, form: ReturnType<typeof eventForm>): Omit<EventAmendment, 'reason'> {
  const changes: Omit<EventAmendment, 'reason'> = {}
  for (const field of Object.keys(form) as Array<keyof typeof form>) {
    if (field === 'posted_at') {
      const timestamp = toEventTimestamp(form.posted_at, event.posted_at)
      if (timestamp !== event.posted_at && new Date(timestamp).getTime() !== new Date(event.posted_at).getTime()) changes.posted_at = timestamp
    } else if (form[field] !== event[field]) Object.assign(changes, { [field]: form[field] })
  }
  return changes
}

export function amendmentMessage(result: EventAmendmentResult): string {
  if (!result.revision.recalculation_required) return '事件已修订，现有收益保持不变'
  if (result.refresh_status === 'queued') return '事件已修订，行情补齐与收益重算已入队'
  if (result.refresh_status === 'disabled') return '事件已修订，旧收益已撤下；自动重算未启用，等待运行收益更新'
  if (result.refresh_status === 'failed') return `事件已修订，旧收益已撤下；重算启动失败${result.refresh_error ? `：${result.refresh_error}` : ''}`
  return '事件已修订，旧收益已撤下；收益仍待重算'
}

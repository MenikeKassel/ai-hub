import { describe, expect, it } from 'vitest'
import type { Event } from './types'
import { eventChanges, eventForm, toEventTimestamp, toLocalDateTimeInput } from './eventEditing'

const event = {
  event_id: 'KOL-T001',
  posted_at: '2026-07-08T10:00:00+08:00',
  symbol: '600000',
  security_name: '浦发银行',
  direction: 'long',
  thesis: '原理由',
} as Event

describe('event timestamp editing', () => {
  it('preserves the original timestamp when the local input represents the same instant', () => {
    const local = toLocalDateTimeInput(event.posted_at)
    expect(toEventTimestamp(local, event.posted_at)).toBe(event.posted_at)
    expect(eventChanges(event, eventForm(event))).not.toHaveProperty('posted_at')
  })

  it('serializes an actual timestamp edit with timezone information', () => {
    const local = toLocalDateTimeInput(event.posted_at)
    const changed = `${local.slice(0, -2)}01`
    expect(toEventTimestamp(changed, event.posted_at)).toMatch(/(Z|[+-]\d{2}:\d{2})$/)
  })
})

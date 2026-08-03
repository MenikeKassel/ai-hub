import { describe, expect, it } from 'vitest'
import { numeric, percentValue } from './EventMethodResearch'

describe('event method research value formatting', () => {
  it('does not turn missing research data into zero', () => {
    expect(numeric(null, 1)).toBe('-')
    expect(numeric(undefined)).toBe('-')
    expect(percentValue(null)).toBe('-')
  })

  it('keeps real zero values visible', () => {
    expect(numeric(0, 1)).toBe('0.0')
    expect(percentValue(0)).toBe('0.00%')
  })
})

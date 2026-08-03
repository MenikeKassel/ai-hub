import { describe, expect, it } from 'vitest'
import { rankBand, rankSegments } from './BoardRankTrendChart'
import type { BoardRankPoint } from '../types'

function point(tradeDate: string, rank: number): BoardRankPoint {
  return {
    trade_date: tradeDate,
    rank,
    universe_size: 90,
    rps: 50,
    period_return: 0,
    status: 'neutral',
    warnings: [],
  }
}

describe('board rank trend segments', () => {
  it('assigns each interval to the band of its ending trading day', () => {
    const segments = rankSegments([
      point('2026-07-20', 35),
      point('2026-07-21', 20),
      point('2026-07-22', 8),
      point('2026-07-23', 7),
      point('2026-07-24', 40),
    ])

    expect(segments.map((segment) => segment.band)).toEqual([
      'upper',
      'top',
      'other',
    ])
    expect(segments[1].points.map((item) => item.rank)).toEqual([20, 8, 7])
    expect(rankBand(10)).toBe('top')
    expect(rankBand(30)).toBe('upper')
    expect(rankBand(31)).toBe('other')
  })
})

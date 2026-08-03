import { useEffect, useMemo, useRef } from 'react'
import {
  ColorType,
  createChart,
  LineSeries,
  type LineData,
  type Time,
} from 'lightweight-charts'
import type { BoardRankPoint, BoardRankSeries } from '../types'

const COLORS = {
  top: '#d92d20',
  upper: '#f79009',
  other: '#2563eb',
  grid: '#eef1f4',
  axis: '#d8dde3',
}

export type RankBand = 'top' | 'upper' | 'other'

export function rankBand(rank: number): RankBand {
  if (rank <= 10) return 'top'
  if (rank <= 30) return 'upper'
  return 'other'
}

export function rankSegments(points: BoardRankPoint[]): Array<{
  band: RankBand
  points: BoardRankPoint[]
}> {
  const segments: Array<{ band: RankBand; points: BoardRankPoint[] }> = []
  for (let index = 1; index < points.length; index += 1) {
    const currentBand = rankBand(points[index].rank)
    const current = segments.at(-1)
    if (current?.band === currentBand) {
      current.points.push(points[index])
    } else {
      segments.push({
        band: currentBand,
        points: [points[index - 1], points[index]],
      })
    }
  }
  return segments
}

export default function BoardRankTrendChart({
  series,
  rangeName = '120',
}: {
  series?: BoardRankSeries
  rangeName?: '120' | '250' | 'all'
}) {
  const container = useRef<HTMLDivElement>(null)
  const points = useMemo(() => series?.points || [], [series?.points])

  useEffect(() => {
    if (!container.current || !points.length || typeof ResizeObserver === 'undefined') return
    const chart = createChart(container.current, {
      autoSize: true,
      height: 270,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#697386',
        attributionLogo: true,
        fontFamily: 'Inter, "Microsoft YaHei", sans-serif',
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      rightPriceScale: {
        borderColor: COLORS.axis,
        invertScale: true,
        scaleMargins: { top: 0.06, bottom: 0.06 },
        minimumWidth: 52,
      },
      timeScale: {
        borderColor: COLORS.axis,
        rightOffset: 5,
        barSpacing: 5,
        minBarSpacing: 1,
      },
      localization: { locale: 'zh-CN', dateFormat: 'yyyy-MM-dd' },
    })

    const anchor = chart.addSeries(LineSeries, {
      color: 'rgba(0,0,0,0)',
      lineWidth: 1,
      title: '排名',
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
      lastValueVisible: true,
      priceLineVisible: false,
      crosshairMarkerVisible: true,
      lineVisible: false,
    })
    const anchorData: LineData<Time>[] = points.map((point) => ({
      time: point.trade_date,
      value: point.rank,
    }))
    anchor.setData(anchorData)

    const widths = { top: 4, upper: 3, other: 2 } as const
    for (const segment of rankSegments(points)) {
      const line = chart.addSeries(LineSeries, {
        color: COLORS[segment.band],
        lineWidth: widths[segment.band],
        title: '',
        priceFormat: { type: 'price', precision: 0, minMove: 1 },
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      })
      line.setData(segment.points.map((point) => ({
        time: point.trade_date,
        value: point.rank,
      })))
    }

    anchor.createPriceLine({ price: 10, color: COLORS.top, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '10' })
    anchor.createPriceLine({ price: 30, color: COLORS.upper, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '30' })
    const tooltip = document.createElement('div')
    tooltip.className = 'board-rank-tooltip'
    tooltip.style.display = 'none'
    container.current.appendChild(tooltip)
    const byDate = new Map(points.map((point) => [point.trade_date, point]))
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point || !container.current) {
        tooltip.style.display = 'none'
        return
      }
      const tradeDate = String(param.time)
      const point = byDate.get(tradeDate)
      if (!point) {
        tooltip.style.display = 'none'
        return
      }
      const periodReturn = point.period_return == null
        ? '-'
        : `${(Number(point.period_return) * 100).toFixed(2)}%`
      const rps = point.rps == null ? '-' : Number(point.rps).toFixed(1)
      tooltip.textContent = [
        point.trade_date,
        `排名 ${point.rank} / ${point.universe_size}`,
        `RPS ${rps} · 周期收益 ${periodReturn}`,
        `状态 ${point.status || '-'}`,
      ].join('\n')
      tooltip.style.display = 'block'
      const width = container.current.clientWidth
      tooltip.style.left = `${Math.max(8, Math.min(param.point.x + 12, width - 200))}px`
      tooltip.style.top = `${Math.max(8, param.point.y - 70)}px`
    })
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [points])

  return <div
    className="board-rank-chart"
    data-inverted-y="true"
    data-rank-bands="1-10:red:4,11-30:orange:3,31+:blue:2"
    data-range={rangeName}
  >
    <div className="board-chart-legend">
      <strong>板块排名趋势 · RPS{series?.window || 50}</strong>
      <span><i className="rank-swatch top" />前10名</span>
      <span><i className="rank-swatch upper" />11-30名</span>
      <span><i className="rank-swatch other" />其他</span>
      <small>
        显示 {series?.display_from || '-'} 至 {series?.display_to || '-'} · {series?.returned_point_count || series?.point_count || 0} 点
        {series?.truncated ? ` / 全部 ${series?.total_point_count || 0} 点` : ''}
      </small>
    </div>
    <div ref={container} className="board-rank-canvas" aria-label="板块排名趋势图" />
    {!points.length && <div className="chart-empty">当前板块还没有可用排名数据</div>}
  </div>
}

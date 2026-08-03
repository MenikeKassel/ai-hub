import { useEffect, useMemo, useRef } from 'react'
import {
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
  type LineData,
  type Time,
} from 'lightweight-charts'
import type { BoardSeriesBar } from '../types'

export default function BoardRpsChart({ rows }: { rows: BoardSeriesBar[] }) {
  const container = useRef<HTMLDivElement>(null)
  const data = useMemo(
    () => [...rows].sort((a, b) => a.trade_date.localeCompare(b.trade_date)),
    [rows],
  )

  useEffect(() => {
    if (!container.current || !data.length || typeof ResizeObserver === 'undefined') return
    const chart = createChart(container.current, {
      autoSize: true,
      height: 220,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#697386',
        attributionLogo: true,
        fontFamily: 'Inter, "Microsoft YaHei", sans-serif',
      },
      grid: {
        vertLines: { color: '#f0f2f4' },
        horzLines: { color: '#e7eaee' },
      },
      rightPriceScale: {
        borderColor: '#d8dde3',
        scaleMargins: { top: 0.08, bottom: 0.08 },
        minimumWidth: 44,
      },
      timeScale: { borderColor: '#d8dde3', rightOffset: 4, barSpacing: 6, minBarSpacing: 2 },
      localization: { locale: 'zh-CN' },
    })
    const definitions = [
      { key: 'rps_50' as const, color: '#b53f3f', title: 'RPS50' },
      { key: 'rps_120' as const, color: '#315f96', title: 'RPS120' },
      { key: 'rps_250' as const, color: '#176b55', title: 'RPS250' },
    ]
    definitions.forEach(({ key, color, title }, index) => {
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        title,
        priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
        lastValueVisible: true,
        priceLineVisible: false,
      })
      const values: LineData<Time>[] = data.flatMap((row) => {
        const value = row[key]
        return value === null || value === undefined
          ? []
          : [{ time: row.trade_date, value: Number(value) }]
      })
      series.setData(values)
      if (index === 0) {
        series.createPriceLine({
          price: 87,
          color: '#966417',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: '87',
        })
      }
    })
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [data])

  return <div className="board-rps-chart">
    <div className="board-chart-legend">
      <strong>板块相对强度</strong>
      <span><i className="rps-swatch rps50" />RPS50</span>
      <span><i className="rps-swatch rps120" />RPS120</span>
      <span><i className="rps-swatch rps250" />RPS250</span>
      <span><i className="rps-swatch threshold" />阈值 87</span>
    </div>
    <div ref={container} className="board-rps-canvas" aria-label="板块 RPS50、RPS120 和 RPS250 曲线" />
    {!data.some((row) => row.rps_50 !== null || row.rps_120 !== null || row.rps_250 !== null)
      && <div className="chart-empty">历史回填完成后显示 RPS 曲线</div>}
  </div>
}

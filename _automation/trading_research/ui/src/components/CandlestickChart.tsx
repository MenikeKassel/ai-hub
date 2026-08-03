import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts'
import type { MarketIndicatorBar } from '../types'

export interface PriceChartMarker {
  date: string
  label: string
  kind: 'recommendation' | 'retrospective'
}

interface CandlestickChartProps {
  bars: MarketIndicatorBar[]
  markers: PriceChartMarker[]
  securityName: string
  symbol: string
}

function markerTradingDate(date: string, tradingDates: string[]): string | null {
  if (!tradingDates.length) return null
  return tradingDates.find((value) => value >= date) || tradingDates.at(-1) || null
}

export default function CandlestickChart({ bars, markers, securityName, symbol }: CandlestickChartProps) {
  const container = useRef<HTMLDivElement>(null)
  const sortedBars = useMemo(
    () => [...bars].filter((bar) => Number(bar.open) > 0 && Number(bar.high) > 0 && Number(bar.low) > 0 && Number(bar.close) > 0)
      .sort((a, b) => a.trade_date.localeCompare(b.trade_date)),
    [bars],
  )
  const hasIndicators = sortedBars.some((bar) => (
    bar.ma5 != null || bar.macd_hist != null || bar.rsi14 != null
  ))
  const [focusBar, setFocusBar] = useState<MarketIndicatorBar | null>(sortedBars.at(-1) || null)

  useEffect(() => {
    setFocusBar(sortedBars.at(-1) || null)
    if (!container.current || !sortedBars.length || typeof ResizeObserver === 'undefined') return

    const chart = createChart(container.current, {
      autoSize: true,
      height: hasIndicators ? 720 : 340,
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
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#d8dde3', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: '#d8dde3', rightOffset: 6, barSpacing: 8, minBarSpacing: 4 },
      localization: { locale: 'zh-CN' },
    })
    const isEtf = /^[15]/.test(symbol)
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#c94643',
      downColor: '#16835f',
      borderUpColor: '#c94643',
      borderDownColor: '#16835f',
      wickUpColor: '#c94643',
      wickDownColor: '#16835f',
      priceFormat: {
        type: 'price',
        precision: isEtf ? 3 : 2,
        minMove: isEtf ? 0.001 : 0.01,
      },
    })
    const data: CandlestickData<Time>[] = sortedBars.map((bar) => ({
      time: bar.trade_date,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    }))
    series.setData(data)

    if (hasIndicators) {
      const lineData = (
        key: 'ma5' | 'ma10' | 'ma20' | 'ma60' | 'macd_dif' | 'macd_dea' | 'rsi14',
      ): LineData<Time>[] => sortedBars.flatMap((bar) => {
        const value = bar[key]
        return value == null || !Number.isFinite(Number(value))
          ? []
          : [{ time: bar.trade_date as Time, value: Number(value) }]
      })
      const addLine = (
        key: 'ma5' | 'ma10' | 'ma20' | 'ma60',
        color: string,
        lineWidth: 1 | 2 | 3 | 4 = 1,
      ) => {
        const value = chart.addSeries(LineSeries, {
          color,
          lineWidth,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        })
        value.setData(lineData(key))
      }
      addLine('ma5', '#d92d20', 1)
      addLine('ma10', '#f79009', 1)
      addLine('ma20', '#2563eb', 1)
      addLine('ma60', '#7a5af8', 2)

      const volumeSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceLineVisible: false,
        lastValueVisible: false,
      }, 1)
      const volumeData: HistogramData<Time>[] = sortedBars.flatMap((bar) => (
        bar.volume == null || !Number.isFinite(Number(bar.volume))
          ? []
          : [{
            time: bar.trade_date as Time,
            value: Number(bar.volume),
            color: Number(bar.close) >= Number(bar.open) ? '#d92d2070' : '#16835f70',
          }]
      ))
      volumeSeries.setData(volumeData)

      const macdHistogram = chart.addSeries(HistogramSeries, {
        priceLineVisible: false,
        lastValueVisible: false,
      }, 2)
      macdHistogram.setData(sortedBars.flatMap((bar): HistogramData<Time>[] => (
        bar.macd_hist == null || !Number.isFinite(Number(bar.macd_hist))
          ? []
          : [{
            time: bar.trade_date as Time,
            value: Number(bar.macd_hist),
            color: Number(bar.macd_hist) >= 0 ? '#d92d2080' : '#16835f80',
          }]
      )))
      const dif = chart.addSeries(LineSeries, {
        color: '#2563eb',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      }, 2)
      dif.setData(lineData('macd_dif'))
      const dea = chart.addSeries(LineSeries, {
        color: '#f79009',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      }, 2)
      dea.setData(lineData('macd_dea'))

      const rsi = chart.addSeries(LineSeries, {
        color: '#7a5af8',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      }, 3)
      rsi.setData(lineData('rsi14'))
      rsi.createPriceLine({
        price: 70,
        color: '#d92d2080',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: '70',
      })
      rsi.createPriceLine({
        price: 30,
        color: '#16835f80',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: '30',
      })

      const panes = chart.panes()
      panes[0]?.setHeight(360)
      panes[1]?.setHeight(100)
      panes[2]?.setHeight(140)
      panes[3]?.setHeight(120)
    }

    const tradingDates = sortedBars.map((bar) => bar.trade_date)
    const chartMarkers = markers.flatMap((marker): SeriesMarker<Time>[] => {
      const time = markerTradingDate(marker.date, tradingDates)
      if (!time) return []
      return [{
        time,
        position: marker.kind === 'recommendation' ? 'aboveBar' : 'belowBar',
        color: marker.kind === 'recommendation' ? '#a76d12' : '#64748b',
        shape: marker.kind === 'recommendation' ? 'arrowDown' : 'circle',
        text: marker.label,
      }]
    }).sort((a, b) => String(a.time).localeCompare(String(b.time)))
    createSeriesMarkers(series, chartMarkers)
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((param) => {
      const value = param.seriesData.get(series)
      if (!value || !('open' in value)) {
        setFocusBar(sortedBars.at(-1) || null)
        return
      }
      const date = typeof param.time === 'string'
        ? param.time
        : param.time && typeof param.time === 'object'
          ? `${param.time.year}-${String(param.time.month).padStart(2, '0')}-${String(param.time.day).padStart(2, '0')}`
          : ''
      setFocusBar({
        symbol,
        trade_date: date,
        open: value.open,
        high: value.high,
        low: value.low,
        close: value.close,
        adjustment: sortedBars.at(-1)?.adjustment || 'qfq',
        provider: 'market_warehouse',
      })
    })
    return () => chart.remove()
  }, [hasIndicators, markers, sortedBars, symbol])

  const digits = /^[15]/.test(symbol) ? 3 : 2
  const formatPrice = (value: number | undefined) => value === undefined ? '-' : Number(value).toFixed(digits)

  return <div className="price-chart">
    <div className="price-chart-toolbar">
      <div className="price-chart-legend">
        <strong>前复权股价 · 日K · qfq</strong>
        <span><i className="price-swatch up" />上涨</span>
        <span><i className="price-swatch down" />下跌</span>
        {hasIndicators && <>
          <span><i className="line-swatch ma5" />MA5</span>
          <span><i className="line-swatch ma10" />MA10</span>
          <span><i className="line-swatch ma20" />MA20</span>
          <span><i className="line-swatch ma60" />MA60</span>
          <span>成交量 · MACD · RSI14</span>
        </>}
      </div>
      {focusBar && <div className="ohlc-readout" aria-live="polite">
        <strong>{focusBar.trade_date}</strong>
        <span>开 {formatPrice(focusBar.open)}</span>
        <span>高 {formatPrice(focusBar.high)}</span>
        <span>低 {formatPrice(focusBar.low)}</span>
        <span>收 {formatPrice(focusBar.close)}</span>
      </div>}
    </div>
    <div ref={container} className={`candlestick-canvas ${hasIndicators ? 'with-indicators' : ''}`} aria-label={`${securityName}前复权股价K线图`} />
    {!sortedBars.length && <div className="chart-empty">暂无日线行情</div>}
  </div>
}

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpenCheck, ChevronDown } from 'lucide-react'
import { api } from '../api'
import type {
  EventMethodLens,
  EventMethodResearchSection,
} from '../types'

const lensOrder = [
  'short_term_leader',
  'dow_wave_gann',
  'price_action',
  'ict',
  'wyckoff_orderflow',
] as const

const warningLabels: Record<string, string> = {
  full_market_cross_section_unavailable: '缺少全市场横截面，不能判定短线龙头',
  symbol_missing_from_cross_section: '事件股票不在该交易日横截面中，短线龙头暂不判定',
  qfq_history_unavailable_at_event: '推荐时点之前没有可用的前复权日线',
  qfq_rebase_raw_cutoff_unavailable: '缺少事件截止日真实收盘价，前复权价格尺度未能重新锚定',
  limit_status_is_approximate_without_st_flag_and_exchange_limit_price: '涨停连板为近似值，尚缺 ST 与交易所涨跌停价',
  wave_count_requires_manual_interpretation: '波浪计数需人工确认',
  gann_rules_not_standardized: '尚未配置唯一、可复现的江恩规则',
  price_only_interpretation_not_signal: 'PA 仅描述价格结构，不构成交易信号',
  ict_interpretation_is_discretionary: 'ICT 结构需人工解释',
  liquidity_levels_are_price_proxies_not_order_book_liquidity: '流动性位是价格代理，不是订单簿流动性',
  wyckoff_phase_requires_manual_interpretation: '威科夫阶段需人工确认',
  cvd_unavailable_without_aggressor_side_trades: '缺逐笔主动买卖方向，不能计算真实 CVD',
  option_wall_unavailable_without_option_open_interest: '缺执行价维度期权持仓，不能计算期权墙',
  option_wall_not_applicable_for_stock: '普通 A 股没有个股期权墙，此项不适用',
  minute_session_unavailable: '尚无推荐时点前的完整分钟会话',
  swing_anchors_fallback_60d: '摆动锚点不足，暂用近60日高低点',
  zero_swing_range: '摆动区间为零，无法计算斐波那契位置',
  limit_status_is_approximate_without_exchange_limit_metadata: '涨停按市场板块和ST状态近似判断，上市初期等特殊规则仍需复核',
  cross_section_history_less_than_21_sessions: '横截面历史不足21个交易日，趋势与容量候选暂不判定',
  cross_section_stale_for_event: '全市场横截面与事件证据日不一致，短线龙头暂不判定',
  board_membership_snapshot_unavailable: '缺少事件时点板块成分股快照',
  board_context_unavailable: '缺少事件时点板块归属或 RPS，容量核心与板块龙暂不判定',
}

export function numeric(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === '') return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '-'
}

export function percentValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(2)}%` : '-'
}

function yesNo(value: unknown): string {
  return value === true ? '是' : value === false ? '否' : '-'
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function lensFacts(key: string, lens: EventMethodLens): Array<[string, string]> {
  const facts = record(lens.facts)
  if (key === 'short_term_leader') {
    const candidates = record(facts.candidate_types)
    const candidateValue = (name: string) => {
      const value = record(candidates[name])
      if (value.candidate === true) return '候选'
      if (value.candidate === false) return '不满足'
      return '待数据'
    }
    return [
      ['结论', lens.conclusion === 'candidate' ? '存在候选类型' : lens.conclusion === 'not_candidate' ? '未满足候选规则' : '待数据补齐'],
      ['连板龙', candidateValue('limit_up_leader')],
      ['趋势龙', candidateValue('trend_leader')],
      ['容量核心', candidateValue('liquidity_core')],
      ['板块龙', candidateValue('board_leader')],
      ['全市场样本', numeric(facts.universe_size, 0)],
      ['20日收益', percentValue(facts.return_20d)],
      ['距60日高点', percentValue(facts.distance_60d_high)],
      ['最强板块 RPS50', numeric(facts.strongest_board_rps50, 1)],
    ]
  }
  if (key === 'dow_wave_gann') {
    const fib = record(facts.fib_context)
    return [
      ['道氏结构', String(facts.trend_structure || '-')],
      ['ATR%', percentValue(facts.atr14_pct)],
      ['MA20', numeric(facts.ma20)],
      ['MA60', numeric(facts.ma60)],
      ['Fib 区域', String(fib.premium_discount || '-')],
      ['波浪 / 江恩', '人工复核'],
    ]
  }
  if (key === 'price_action') {
    return [
      ['价格环境', String(facts.regime || '-')],
      ['结构', String(facts.trend_structure || '-')],
      ['20日方向效率', percentValue(facts.directional_efficiency_20)],
      ['K线重叠', percentValue(facts.bar_overlap_20)],
      ['20日突破', String(facts.breakout_vs_prior_20 || '-')],
      ['收盘位置', percentValue(facts.latest_close_location)],
    ]
  }
  if (key === 'ict') {
    const dealingRange = record(facts.dealing_range)
    return [
      ['发帖时段', String(facts.post_session || '-')],
      ['距前高', percentValue(facts.distance_to_prior_20_high)],
      ['距前低', percentValue(facts.distance_to_prior_20_low)],
      ['扫买方流动性', yesNo(facts.buy_side_liquidity_sweep)],
      ['扫卖方流动性', yesNo(facts.sell_side_liquidity_sweep)],
      ['Fib 区域', String(dealingRange.premium_discount || '-')],
    ]
  }
  const wyckoff = record(facts.wyckoff)
  const vwap = record(facts.vwap)
  const volumeProfile = record(facts.volume_profile)
  const tpo = record(facts.tpo)
  const cvd = record(facts.cvd)
  const optionWall = record(facts.option_wall)
  return [
    ['量价努力 / 结果', String(wyckoff.effort_result || '-')],
    ['CLV', numeric(wyckoff.close_location_value)],
    ['价差 / ATR', numeric(wyckoff.spread_atr)],
    ['会话 VWAP', numeric(vwap.session_vwap)],
    ['VP POC（近似）', numeric(volumeProfile.poc)],
    ['TPO POC（近似）', numeric(tpo.poc)],
    ['真实 CVD', cvd.status === 'unavailable' ? '不可用' : String(cvd.status || '不可用')],
    ['期权墙', optionWall.status === 'not_applicable' ? '不适用' : '不可用'],
  ]
}

function statusLabel(status: string): string {
  if (status === 'ready') return '数据完整'
  if (status === 'partial') return '部分可用'
  if (status === 'failed') return '计算失败'
  return '暂无数据'
}

export function MethodResearchCards({
  section,
}: {
  section?: EventMethodResearchSection | null
}) {
  const research = section?.data
  if (!research) {
    return <div className="method-research-empty">研究视角尚未生成；补齐前复权日线后可重新计算。</div>
  }
  const interpretations = section?.interpretation?.payload?.interpretations || []
  const interpretationByLens = new Map(
    interpretations.map((item) => [item.lens, item]),
  )
  const interpretationFailed = section?.interpretation?.status === 'failed'
  const priceScaleBasis = research.data_lineage.daily_price_scale_basis
  return <div className="method-research">
    <div className="method-research-meta">
      <span>证据时点 <strong>{research.as_of_trade_date || '-'}</strong></span>
      <span>版本 <strong>{research.version}</strong></span>
      <span>价格尺度 <strong>{
        priceScaleBasis === 'event_cutoff_raw_close'
          ? '事件日真实收盘锚定'
          : '供应商前复权'
      }</strong></span>
      <span>分钟柱 <strong>{String(research.data_lineage.minute_bars ?? 0)}</strong></span>
      <span>AI研判 <strong>{
        interpretationFailed
          ? '校验失败'
          : section?.interpretation
            ? section.interpretation.provider
            : '待处理'
      }</strong></span>
      {section?.interpretation && <>
        <span>策略 <strong>{section.interpretation.prompt_version}</strong></span>
        <span>证据校验 <strong>{
          section.interpretation.validation?.ok ? '通过' : '未通过'
        }</strong></span>
        <span>生成时间 <strong>{
          section.interpretation.created_at.replace('T', ' ').slice(0, 16)
        }</strong></span>
      </>}
    </div>
    {interpretationFailed && <div className="error-banner compact">
      AI解释未通过证据校验：{section?.interpretation?.error || '输出不符合研究Schema'}。客观数据仍然有效，可由后台安全重试。
    </div>}
    <div className="method-lens-grid">
      {lensOrder.map((key) => {
        const lens = research.lenses[key]
        if (!lens) return null
        const interpretation = interpretationByLens.get(key)
        return <section className="method-lens" key={key}>
          <header>
            <strong>{lens.label}</strong>
            <span className={`badge ${lens.status === 'ready' ? 'green' : lens.status === 'failed' ? 'red' : 'amber'}`}>
              {statusLabel(lens.status)}
            </span>
          </header>
          <div className="method-lens-facts">
            {lensFacts(key, lens).map(([label, value]) => <div key={label}>
              <span>{label}</span><strong>{value}</strong>
            </div>)}
          </div>
          {lens.observations.length > 0 && <p className="method-observations">{lens.observations.join(' · ')}</p>}
          {interpretation && <div className="method-ai">
            <div><span>AI假设</span><strong>{Math.round(interpretation.confidence * 100)}%</strong></div>
            <p>{interpretation.hypothesis}</p>
            <small>证据：{interpretation.evidence_refs.join(' · ')}</small>
            {interpretation.counter_evidence.length > 0 && <small>反证：{interpretation.counter_evidence.join('；')}</small>}
            {interpretation.invalidation.length > 0 && <small>失效：{interpretation.invalidation.join('；')}</small>}
          </div>}
          {!interpretation && <p className="method-ai-pending">AI解释待处理；客观数据不受影响。</p>}
          {lens.warnings.length > 0 && <ul className="method-limitations">
            {lens.warnings.map((warning) => <li key={warning}>{warningLabels[warning] || warning}</li>)}
          </ul>}
        </section>
      })}
    </div>
    <p className="method-research-note">
      各方法仅提供推荐时点的可审计上下文；不合成技术分，不改变 KOL 收益，也不生成买卖建议。
    </p>
  </div>
}

export default function EventMethodResearch({
  eventId,
}: {
  eventId: string
}) {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: ['event-method-research', eventId],
    queryFn: () => api.eventResearch(eventId),
    enabled: open,
  })
  return <details
    className="event-method-research"
    onToggle={(event) => setOpen(event.currentTarget.open)}
  >
    <summary>
      <span><BookOpenCheck size={14} />多方法研究</span>
      <small>短线龙头 · 道氏/波浪 · PA · ICT · 威科夫/订单流</small>
      <ChevronDown size={14} />
    </summary>
    {query.isLoading && <div className="loading-state compact">正在计算推荐时点研究视角...</div>}
    {query.isError && <div className="error-banner">研究视角读取失败：{String(query.error)}</div>}
    {query.data && <MethodResearchCards section={query.data} />}
  </details>
}

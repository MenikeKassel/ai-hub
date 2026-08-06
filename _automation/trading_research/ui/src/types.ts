export type ReviewStatus = 'pending' | 'approved' | 'excluded' | 'ignored' | 'capture_failed'

export interface Kol {
  id: number
  display_name: string
  platform: 'X' | 'Zhihu'
  handle: string
  profile_url: string
  domain: string
  status: 'active' | 'paused'
  tracking_mode: string
  last_post_id: string
  last_fetched_at: string
  last_success_at: string
  last_gap_at: string
  consecutive_failures: number
  backfill_requested: number
  backfill_status: string
  backfill_completed_depth: number
  backfill_result_count: number
  backfill_warning: string
  fetch_status: string
}

export interface DigestAuthor {
  author_name: string
  normalized_name: string
  status: 'attributed_only'
  source_kind: 'secondhand_aggregation'
  summary_count: number
  latest_posted_at: string
  latest_source_url: string
  latest_title: string
  latest_summary: string
  symbols: string[]
  profile_handle: string
  profile_url: string
  tracking_status: 'unresolved' | 'linked_only' | 'paused' | 'active'
  profile_updated_at: string
}

export interface KolHorizonMetrics {
  samples: number
  median_return: number | null
  mean_return: number | null
  median_excess: number | null
  mean_excess: number | null
  win_rate: number | null
  median_adverse: number | null
}

export interface KolLeaderboardRow {
  kol_name: string
  tier: 'collecting' | 'watch' | 'provisional' | 'reliable' | 'long_term'
  rank: number | null
  rank_horizon: '1W' | '1M' | '3M' | '6M'
  score: number | null
  event_count: number
  executable_event_count: number
  long_event_count: number
  short_event_count: number
  executable_long_event_count: number
  audit_event_count: number
  horizons: Record<'1W' | '1M' | '3M' | '6M', KolHorizonMetrics>
}

export interface KolLeaderboard {
  policy: Record<string, string>
  rows: KolLeaderboardRow[]
}

export interface KolPerformanceMetrics extends KolHorizonMetrics {
  batch_count: number
  event_count: number
  long_event_count: number
  short_event_count: number
  executable_long_event_count: number
  audit_event_count: number
  recommendation_days: number
  unique_symbols: number
  unmatured_batch_count: number
  median_mae: number | null
  median_mfe: number | null
  worst_batch_excess: number | null
  p10_excess: number | null
  confidence_interval: { lower: number; upper: number } | null
  sample_status: string
}

export interface KolPerformanceRow {
  kol_key: string
  kol_id: string
  kol_handle: string
  kol_name: string
  platform: string
  tier: 'collecting' | 'watch' | 'provisional' | 'reliable' | 'long_term'
  tier_label: string
  rank: number | null
  rank_horizon: '1W' | '1M' | '3M' | '6M'
  horizon: '1W' | '1M' | '3M' | '6M'
  window: string
  metrics: KolPerformanceMetrics
  horizons: Record<'1W' | '1M' | '3M' | '6M', KolPerformanceMetrics>
  primary_only: boolean
  narrative?: {
    summary: string
    strengths: string[]
    risks: string[]
    changes: string[]
    limitations: string[]
    evidence_refs: string[]
    provider: string
    model: string
    status: string
  }
}

export interface KolPerformanceResponse {
  version: string
  as_of: string
  platform: string
  window: string
  horizon: '1W' | '1M' | '3M' | '6M'
  primary_only: boolean
  coverage: {
    total_events: number
    primary_events: number
    kol_count: number
    ranked_count: number
    mature_batch_count: number
    unmatured_batch_count: number
    sample_note: string
  }
  rows: KolPerformanceRow[]
  secondary: { description: string; rows: KolPerformanceRow[] }
  run_id?: string
  snapshots_inserted?: number
}

export interface KolPerformanceDetail {
  version: string
  as_of: string
  row: KolPerformanceRow
  series: Record<string, Array<{ as_of: string; payload: KolPerformanceRow }>>
}

export interface LocalMedia {
  type: string
  path: string
  api_url: string
  sha256: string
  bytes: number
}

export interface Draft {
  symbol: string
  security_name: string
  direction: 'long' | 'short'
  action?: 'buy' | 'add' | 'hold' | 'watch' | 'reduce' | 'sell' | 'avoid'
  horizon?: 'intraday' | 'short' | 'swing' | 'medium_long' | 'unspecified'
  strength?: 'explicit' | 'moderate' | 'weak' | 'unspecified'
  thesis: string
  evidence_type: 'original_pre_event' | 'retrospective' | 'secondhand' | 'ambiguous'
  confidence: number
  evidence_spans?: string[]
  evidence_source?: 'text' | 'article_text' | 'quoted_text' | 'ocr' | 'image'
  conditions?: string[]
  depends_on_ocr?: boolean
  mention_kind?: 'recommendation' | 'holding' | 'retrospective' | 'analysis'
}

export type RecommendationDraftStatus = 'ready' | 'needs_attention' | 'approved' | 'rejected' | 'superseded'
export type DraftCorrectionType = 'missed_stock' | 'wrong_mapping' | 'wrong_direction' | 'wrong_thesis' | 'wrong_evidence' | 'wrong_content_type'

export interface RecommendationDraft extends Draft {
  id: number
  post_id: string
  status: RecommendationDraftStatus
  attention_reasons: string[]
  evidence_spans: string[]
  conditions: string[]
  queue_scope: 'morning' | 'backlog'
  review_date: string
  review_note: string
  event_id: string
  reviewed_at: string
  extraction_version: string
  model_name: string
  platform?: 'X' | 'Zhihu'
  url: string
  text: string
  article_title: string
  article_text: string
  quoted_text: string
  ocr_text: string
  posted_at: string
  post_type: string
  provider_warning: string
  review_status: ReviewStatus
  display_name: string
  handle: string
  local_media: LocalMedia[]
}

export interface DraftRevision {
  id: number
  draft_id: number
  post_id: string
  correction_type: DraftCorrectionType
  note: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  actor: string
  created_at: string
}

export interface ManualRecommendationDraft {
  symbol: string
  security_name: string
  direction: 'long' | 'short'
  action: 'buy' | 'add' | 'hold' | 'watch' | 'reduce' | 'sell' | 'avoid'
  horizon: 'intraday' | 'short' | 'swing' | 'medium_long' | 'unspecified'
  strength: 'explicit' | 'moderate' | 'weak' | 'unspecified'
  thesis: string
  evidence_spans: string[]
  conditions: string[]
  evidence_source: 'text' | 'ocr' | 'image'
  depends_on_ocr: boolean
  review_date: string
  correction_type: 'missed_stock'
  note: string
}

export interface MorningReview {
  review_date: string
  summary: {
    new_posts: number
    ai_processed: number
    waiting_review: number
    ai_failed: number
    approved_today: number
  }
  delivery?: {
    status: 'pending' | 'ready' | 'degraded' | 'late' | 'missed'
    deadline: string
    completed_at: string
    coverage: number
    active_kols: number
    successful_kols: number
    failed_kols: number
    errors: string[]
    latest_run_id: string
    latest_phase: string
    latest_status: string
    stage: string
    progress_current: number
    progress_total: number
  }
  posts: Post[]
  drafts: RecommendationDraft[]
  history_drafts: RecommendationDraft[]
  history_page: number
  history_page_size: number
  history_has_more: boolean
  approved_drafts: RecommendationDraft[]
}

export type ReviewAgentDecisionKind = 'auto_approve' | 'auto_exclude' | 'auto_ignore' | 'needs_human' | 'failed'
export type ReviewAgentDecisionStatus = 'proposed' | 'applied' | 'overridden' | 'rolled_back' | 'failed'

export interface ReviewAgentDecision {
  id: number
  run_id: string
  post_id: string
  mode: 'shadow' | 'enabled'
  policy_version: string
  input_hash: string
  decision: ReviewAgentDecisionKind
  confidence: number
  reason_codes: string[]
  evidence: string[]
  drafts: Draft[]
  validator_errors: string[]
  status: ReviewAgentDecisionStatus
  event_ids: string[]
  created_event_ids?: string[]
  side_effects?: { confirmed_lead_ids?: number[] }
  model_name: string
  prompt_version: string
  created_at: string
  applied_at: string
  overridden_at: string
  rolled_back_at: string
  url: string
  text: string
  posted_at: string
  review_status: ReviewStatus
  display_name: string
  handle: string
}

export interface ReviewAgentSummary {
  settings: {
    mode: 'shadow' | 'enabled'
    policy_version: string
    shadow_started_at: string
    enabled_at: string
    updated_at: string
  }
  decision_counts: Record<ReviewAgentDecisionKind, number>
  validation_decision_counts: Record<ReviewAgentDecisionKind, number>
  status_counts: Record<ReviewAgentDecisionStatus, number>
  total_decisions: number
  validation_total_decisions: number
  shadow_days: number
  comparable_decisions: number
  comparable_approvals: number
  agreement: number
  approval_agreement: number
  critical_error_count: number
  activation_ready: boolean
  last_run: null | {
    run_id: string
    mode: string
    policy_version: string
    started_at: string
    completed_at: string
    status: string
    processed_count: number
    auto_approved: number
    auto_excluded: number
    auto_ignored: number
    needs_human: number
    failed: number
  }
}

export interface ReviewResult {
  post_id: string
  event_ids?: string[]
  review_status?: ReviewStatus
  source_ref?: string
  created_events?: number
  queued_symbols?: string[]
  refresh_status?: 'not_requested' | 'queued'
}

export interface Post {
  post_id: string
  kol_id: number
  display_name: string
  domain: string
  handle: string
  platform?: 'X' | 'Zhihu'
  author_name: string
  url: string
  text: string
  article_title: string
  article_text: string
  quoted_text: string
  quoted_author: string
  reply_to_id: string
  reply_to_author: string
  posted_at: string
  post_type: 'original' | 'quote' | 'retweet'
  media: Array<{ type: string; url: string }>
  local_media: LocalMedia[]
  metrics: Record<string, number>
  review_status: ReviewStatus
  review_note: string
  rule_score: number | null
  rule_reasons: string[]
  rule_symbols: string[]
  rule_direction: string
  content_type: string
  evidence_type: string
  is_candidate: boolean
  model_status: string
  model_name: string
  confidence: number
  model_summary: string
  drafts: Draft[]
  draft_generation_status?: 'pending' | 'generated' | 'not_applicable' | 'needs_attention' | 'failed'
  draft_generation_error?: string
  model_error: string
  ocr_status: string
  ocr_attempts: number
  ocr_text: string
  ocr_error: string
  ocr_updated_at: string
  source_note: string
  notion_url: string
  canonical_provider: string
  metrics_provider: string
  provider_warning: string
}

export interface StockLead {
  id: number
  post_id: string
  kol_id: number
  display_name: string
  handle: string
  url: string
  text: string
  posted_at: string
  review_status: ReviewStatus
  symbol: string
  security_name: string
  instrument_type: 'stock' | 'etf' | 'index'
  direction: string
  mention_kind: string
  evidence_text: string
  extraction_method: string
  confidence: number
  status: 'pending' | 'confirmed' | 'ignored'
  auto_confirmed: boolean
  review_note: string
  article_text?: string
  ocr_text?: string
  related_event_ids?: string[]
}

export interface StockMentionPage {
  items: StockLead[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface Instrument {
  symbol: string
  name: string
  instrument_type: 'stock' | 'etf' | 'index'
  exchange: 'SH' | 'SZ'
  status: string
  list_date: string
  lifecycle: 'pinned' | 'tracking' | 'archived'
  source: string
  first_seen_at: string
  last_mentioned_at: string
  updated_at: string
}

export interface MarketCoverage {
  symbol: string
  dataset: string
  adjustment: string
  provider: string
  start_date: string
  end_date: string
  row_count: number
  quality_status: string
  paths: string[]
  updated_at: string
}

export interface QualityIssue {
  run_id: string
  symbol: string
  code: string
  severity: 'warning' | 'error'
  message: string
  affected_rows: number
  created_at: string
}

export interface MarketRun {
  run_id: string
  dataset: string
  provider: string
  symbol: string
  adjustment: string
  status: string
  row_count: number
  quality_status: string
  started_at: string
  completed_at: string
  error: string
}

export interface MarketHealth {
  ok: boolean
  database: string
  instrument_count: number
  active_instruments: number
  coverage_count: number
  warning_count: number
  recent_issue_count: number
  latest_open_date?: string
  latest_daily_date?: string
  daily_data_status?: 'current' | 'provider_pending'
  lagging_symbols?: string[]
  lagging_symbol_count?: number
  coverage?: MarketCoverage[]
  issues?: QualityIssue[]
  runs?: MarketRun[]
  queue?: Array<{ queue_key: string; symbol: string; status: string; reason: string; last_error: string }>
  freestockdb?: FreeStockDBHealth
}

export interface FreeStockDBHealth {
  ok: boolean
  service_ok?: boolean
  data_fresh?: boolean
  update_ready?: boolean
  storage_migrated?: boolean
  storage_layout?: {
    status?: 'local' | 'canonical' | 'reversed' | 'not_migrated' | 'missing_compatibility_link' | 'conflict'
    canonical?: boolean
    data_is_link?: boolean
    live_is_link?: boolean
    same_target?: boolean
  }
  root?: string
  base_url?: string
  service_status?: string
  port?: number
  port_open?: boolean
  port_conflict?: boolean
  transport_warning?: string
  catalog_symbols?: number
  catalog_groups?: number
  manifest?: { file_count?: number; generated_at?: string; exists?: boolean }
  disk?: { free_gb?: number; data_gb?: number; required_for_safe_update_gb?: number; staging_strategy?: string; guard_ok?: boolean; update_guard_ok?: boolean }
  provider?: {
    ok?: boolean
    samples?: Record<string, { rows?: number; latest?: string; error?: string }>
    freshness?: { status?: string; expected_trade_date?: string; stale_symbols?: string[] }
  }
  freshness?: {
    status?: string
    expected_trade_date?: string
    expected_trade_date_source?: string
    stale_symbols?: string[]
  }
  last_update?: {
    ok?: boolean
    status?: string
    phase?: string
    started_at?: string
    completed_at?: string
    expected_trade_date?: string
    error?: string
    acceptance?: { cross_section_coverage?: number; cross_section_symbols?: number }
  }
  error?: string
}

export interface EventIntradayContext {
  snapshot_id?: string
  event_id: string
  symbol?: string
  direction?: string
  posted_at?: string
  effective_start?: string
  window_end?: string
  provider?: string
  frequency?: string
  adjustment?: string
  first_bar_at?: string
  first_price?: number | null
  close_5m?: number | null
  close_15m?: number | null
  close_30m?: number | null
  close_60m?: number | null
  close_window?: number | null
  mfe?: number | null
  mae?: number | null
  bars?: number
  status: string
  warnings?: string[]
  source_hash?: string
  computed_at?: string
}

export interface EventMark {
  trade_date: string
  close_raw: string
  benchmark_close: string
  raw_return: string
  directional_return: string
  benchmark_return: string
  directional_excess_return: string
  max_adverse_return: string
  max_favorable_return?: string
  tracking_days: string
}

export interface EventTechnicalContext {
  snapshot_id: string
  event_id: string
  feature_version: string
  input_hash: string
  symbol: string
  posted_at: string
  expected_trade_date: string
  as_of_trade_date: string
  adjustment: 'qfq'
  rsi14: number | null
  macd_dif: number | null
  macd_dea: number | null
  macd_hist: number | null
  macd_hist_pct: number | null
  atr14: number | null
  atr14_pct: number | null
  volume_ratio_5: number | null
  return_20d: number | null
  distance_60d_high: number | null
  history_bars: number
  status: 'complete' | 'partial' | 'pending' | 'failed'
  warnings: string[]
  source_hash: string
  error: string
  computed_at: string
}

export interface MarketDailyBar {
  symbol: string
  trade_date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  amount?: number
  adjustment: string
  provider: string
}

export interface MarketIndicatorBar extends MarketDailyBar {
  trade_status?: string | number | null
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi14?: number | null
  atr14?: number | null
  atr14_pct?: number | null
  volume_ratio_5?: number | null
  return_20d?: number | null
  distance_60d_high?: number | null
  indicator_version?: string
}

export interface MarketIndicatorSeries {
  symbol: string
  adjustment: 'qfq'
  formula_version: string
  available_from: string
  available_to: string
  row_count: number
  rows: MarketIndicatorBar[]
}

export type BoardType = 'industry' | 'concept'
export type BoardMainlineStatus =
  | 'persistent_candidate'
  | 'mainline_candidate'
  | 'strong_watch'
  | 'rps_only'
  | 'partial_universe'
  | 'neutral'

export interface BoardMainlineItem {
  board_key: string
  board_code: string
  board_name: string
  board_type: BoardType
  trade_date: string
  close: number
  turnover: number | null
  up_count: number | null
  down_count: number | null
  leader_name: string
  leader_change: number | null
  return_50: number | null
  return_120: number | null
  return_250: number | null
  rps_50: number | null
  rps_120: number | null
  rps_250: number | null
  breadth: number | null
  turnover_ratio_20: number | null
  status: BoardMainlineStatus
  coverage_ratio: number
  warnings: string[]
}

export interface BoardMainlinePage {
  items: BoardMainlineItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface BoardSeriesBar {
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number
  volume: number | null
  amount: number | null
  turnover: number | null
  up_count: number | null
  down_count: number | null
  source_kind: string
  rps_50: number | null
  rps_120: number | null
  rps_250: number | null
  breadth: number | null
  turnover_ratio_20: number | null
  status: BoardMainlineStatus | null
  warnings: string[]
  rank_50?: number | null
  rank_120?: number | null
  rank_250?: number | null
  universe_size_50?: number | null
  universe_size_120?: number | null
  universe_size_250?: number | null
}

export interface BoardRankPoint {
  trade_date: string
  rank: number
  universe_size: number
  rps: number | null
  period_return: number | null
  status: BoardMainlineStatus | null
  warnings: string[]
}

export interface BoardRankSeries {
  board_code: string
  board_name: string
  board_type: BoardType
  window: 50 | 120 | 250
  range?: '120' | '250' | 'all'
  available_from: string
  available_to: string
  display_from: string
  display_to: string
  total_point_count: number
  returned_point_count: number
  point_count: number
  truncated: boolean
  points: BoardRankPoint[]
}

export interface BoardRelatedEvent {
  event_id: string
  kol_name: string
  platform: string
  posted_at: string
  symbol: string
  security_name: string
  direction: 'long' | 'short'
  status: string
  source_url: string
}

export interface BoardMainlineDetail {
  board_key: string
  board_code: string
  board_name: string
  board_type: BoardType
  status: string
  provider: string
  first_seen_at: string
  last_seen_at: string
  updated_at: string
  latest: BoardMainlineItem | null
  members: Array<{ symbol: string; security_name: string; snapshot_date: string }>
  related_events: BoardRelatedEvent[]
}

export interface BoardMainlineHealth {
  status: 'empty' | 'backfilling' | 'ready' | 'partial_coverage' | 'source_blocked' | 'failed'
  formula_version: string
  catalog_counts: Partial<Record<BoardType, number>>
  rps_counts: Partial<Record<BoardType, number>>
  coverage_ratios: Partial<Record<BoardType, number>>
  latest_trade_dates: Partial<Record<BoardType, string>>
  pending_backfill: number
  latest_run: null | {
    run_id: string
    operation: string
    status: string
    processed: number
    succeeded: number
    failed: number
    started_at: string
    completed_at: string
    error: string
  }
}

export interface Event {
  event_id: string
  kol_name: string
  platform: string
  kol_id?: string
  kol_handle?: string
  source_post_id?: string
  source_url: string
  source_note: string
  posted_at: string
  symbol: string
  security_name: string
  direction: 'long' | 'short'
  thesis: string
  status: string
  exclusion_reason: string
  baseline_rule: string
  baseline_date: string
  baseline_price_raw: string
  benchmark_symbol: string
  execution_warning: string
  activated_at: string
  updated_at: string
  latest_mark?: EventMark
  technical_context?: EventTechnicalContext | null
  intraday_context?: EventIntradayContext | null
  followups?: EventFollowup[]
}

export interface EventDossierSection {
  status: 'ready' | 'partial' | 'pending' | 'unavailable' | 'failed'
  warnings?: string[]
  [key: string]: unknown
}

export interface EventMethodLens {
  status: 'ready' | 'partial' | 'unavailable' | 'failed'
  label: string
  conclusion: string
  facts: Record<string, unknown>
  observations: string[]
  warnings: string[]
}

export interface EventMethodResearch {
  version: string
  event_id: string
  symbol: string
  posted_at: string
  as_of_trade_date: string
  status: 'ready' | 'partial' | 'unavailable' | 'failed'
  lenses: Record<string, EventMethodLens>
  data_lineage: Record<string, unknown>
  warnings: string[]
  computed_at: string
}

export interface EventMethodInterpretation {
  lens: string
  hypothesis: string
  confidence: number
  evidence_refs: string[]
  counter_evidence: string[]
  invalidation: string[]
}

export interface EventMethodInterpretationRecord {
  interpretation_id: string
  provider: string
  model: string
  prompt_version: string
  status: 'ready' | 'failed'
  payload: {
    interpretations?: EventMethodInterpretation[]
  }
  validation?: { ok: boolean; errors?: string[] }
  error?: string
  created_at: string
}

export interface EventMethodResearchSection extends EventDossierSection {
  data: EventMethodResearch | null
  interpretation?: EventMethodInterpretationRecord | null
  snapshot_id?: string
}

export interface EventDossier {
  event: Event
  status: 'ready' | 'partial' | 'pending' | 'unavailable' | 'failed'
  sections: Record<string, EventDossierSection>
  section_status: Record<string, string>
  completeness: { ready: number; total: number }
  warnings: string[]
  generated_at: string
  snapshot?: { snapshot_id: string; input_hash: string; status: string; created_at: string }
}

export interface EventFollowup {
  post_id: string
  url: string
  text: string
  posted_at: string
  display_name: string
  evidence_type: 'retrospective'
  review_status: ReviewStatus
}

export interface EventUpdate {
  action: 'activate' | 'exclude' | 'restore' | 'archive'
  kol_name?: string
  platform?: string
  source_url?: string
  source_note?: string
  posted_at?: string
  symbol?: string
  security_name?: string
  direction?: 'long' | 'short'
  thesis?: string
  exclusion_reason?: string
}

export interface EventAmendment {
  reason: string
  kol_name?: string
  platform?: string
  source_url?: string
  source_note?: string
  posted_at?: string
  symbol?: string
  security_name?: string
  direction?: 'long' | 'short'
  thesis?: string
}

export interface EventRevision {
  revision_id: string
  event_id: string
  reason: string
  changed_fields: string[]
  before: Record<string, string>
  after: Record<string, string>
  recalculation_required: boolean
  backup_paths?: Record<string, string>
  created_at: string
}

export interface EventAmendmentResult {
  event: Event
  revision: EventRevision
  refresh_status: 'not_required' | 'queued'
}

export interface Checkpoint {
  event_id: string
  horizon: string
  target_days: string
  trade_date: string
  directional_return: string
  directional_excess_return: string
  max_adverse_return: string
  max_favorable_return?: string
  verification_status: string
  finalized_at: string
}

export interface Summary {
  total_posts: number
  pending_posts: number
  actionable_posts: number
  screened_pending_posts: number
  classification_pending: number
  candidate_posts: number
  approved_posts: number
  stock_leads: number
  pending_stock_leads: number
  confirmed_stock_leads: number
  active_kols: number
  active_events: number
  completed_events: number
  checkpoint_count: number
  market?: MarketHealth
  last_fetch_run?: FetchRun
}

export interface FetchRun {
  run_id: string
  started_at: string
  completed_at: string
  status: string
  requested_count: number
  total_kols: number
  processed_kols: number
  stage: string
  successful_kols: number
  failed_kols: number
  new_posts: number
  candidate_posts: number
  errors: string[]
  fallback_kols: string[]
}

export interface Health {
  ok: boolean
  twitter_cli: string
  twitter_credentials_configured: boolean
  twitter_auth_status: string
  zhihu_capture_available: boolean
  zhihu_active_kols: number
  zhihu_paused_kols: number
  zhihu_failed_kols: number
  zhihu_failure_handles: string[]
  zhihu_fetch_status: string
  codex_cli: string
  deepseek_credentials_configured: boolean
  deepseek_provider?: string
  deepseek_model: string
  unlimited_ocr_root: string
  unlimited_ocr_available: boolean
  rapid_ocr_runtime: string
  rapid_ocr_available: boolean
  ocr_provider: string
  database: string
  media_root: string
  media_bytes: number
  post_fetch_task: string
  return_task: string
  market_sync_task: string
  classification_task: string
  review_agent_task: string
  morning_pipeline_task?: string
  morning_runs?: Array<{
    run_id: string
    review_date: string
    status: string
    started_at: string
    completed_at: string
    reviewed_posts: number
    ready_drafts: number
    attention_drafts: number
    failed_posts: number
    phase: string
    active_kols: number
    successful_kols: number
    failed_kols: number
    stage: string
    progress_current: number
    progress_total: number
    errors: string[]
  }>
  digest_task: string
  nitter_credentials_configured: boolean
  nitter_url: string
  nitter_ready: boolean
  nitter_status: string
  nitter_http_status: number
  redis_ready: boolean
  docker_installed: boolean
  docker_ready: boolean
  docker_version: string
  xtf_command: string
  xtf_version: string
  nitter_start_task: string
  twitter_success_rate: number | null
  nitter_attempt_count: number
  fallback_mode: 'shadow' | 'enabled'
  market: MarketHealth
  pipeline_refresh: PipelineRefresh
  review_agent: ReviewAgentSummary
  component_status?: {
    hermes_gateway: { status: string; pid: string }
    operator: { status: string }
    x: { status: string; error_code: string }
    nitter: { status: string; ready: boolean }
    ai: { status: string }
    market: { status: string; ok: boolean }
    freestockdb: { status: string; ok: boolean }
  }
  shadow_rollout: {
    ready: boolean
    required_runs: number
    threshold: number
    runs: Array<{ run_id: string; min_coverage: number; avg_coverage: number }>
  }
}

export interface PipelineRefresh {
  run_id?: string
  status: 'idle' | 'queued' | 'running' | 'deferred' | 'completed' | 'failed'
  phase?: 'market_sync' | 'return_update' | 'busy' | 'done' | 'failed'
  symbols: string[]
  as_of?: string
  error: string
}

export interface PipelineStatus {
  as_of: string
  latest_fetch_at: string
  latest_fetch_status: string
  latest_ai_at: string
  pending_ai: number
  failed_ai: number
  pending_human_review: number
  queue_review_date: string
  queue_kind: 'today' | 'next_preview'
  morning_delivery: MorningReview['delivery'] | null
  next_preview: MorningReview['summary']
  latest_trade_date: string
  expected_trade_date: string
  market_status: 'pre_open' | 'trading' | 'closed' | 'current' | 'provider_pending' | 'unknown'
  lagging_symbols: string[]
  refresh: PipelineRefresh
}

export interface OperatorTasks {
  tasks: Array<{ id: 'morning' | 'fetch' | 'zhihu'; task: string; status: string }>
  fetch: FetchRun | null
  morning: NonNullable<Health['morning_runs']>[number] | null
}

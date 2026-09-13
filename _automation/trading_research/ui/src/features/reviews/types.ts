import type { MorningReview, Post, RecommendationDraft } from '../../types'

export type ReviewView = 'new' | 'processed' | 'pending' | 'failed' | 'approved'
export type QueueScope = 'morning' | 'backlog'
export interface ReviewQueueRow {
  post_id: string; posted_at: string; platform: string; handle: string; display_name: string
  excerpt: string; model_status: string; draft_count: number; attention_count: number; failure_kind: string
}
export interface ReviewQueuePage {
  review_date: string; scope: QueueScope; view: ReviewView; page: number; page_size: number
  total_posts: number; total_drafts: number; has_more: boolean
  counts: Record<ReviewView, number>; items: ReviewQueueRow[]; delivery: MorningReview['delivery']
}
export interface ReviewDetail { post: Post; drafts: RecommendationDraft[] }

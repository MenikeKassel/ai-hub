import { request } from '../../http'

async function readList<T extends { items: unknown[] }>(url: string): Promise<T> {
  const value = await request<T>(url)
  if (!value || !Array.isArray(value.items)) throw new Error('研究数据暂不可用，请稍后重试')
  return value
}

export interface ResearchEvidence { field: string; text: string; kind?: string; source_role?: string }
export interface SurfaceCoverage {
  status: string; gap_count: number; target_surfaces: number; fresh_surfaces: number
  items: Array<{ kol_id: number; display_name: string; handle: string; surface: string; status: string; fresh: boolean; recorded_at: string; received_count: number; origin: string }>
}
export interface ResearchItem {
  id: number; kind: string; theme_name: string; detected_at: string; acknowledged_at: string
  payload: { author_name: string; url: string; posted_at: string; observed_at: string; source_updated_at: string; edited: boolean; resolved_at?: string; evidence_kind?: string; source_role?: string; evidence: ResearchEvidence[] }
}
export interface TopicCandidate {
  id: number; term: string; evidence_count: number
  evidence: Array<{ author_name: string; url: string; posted_at: string; kind: string; source_role: string; spans: ResearchEvidence[] }>
}
export interface SourceVersion {
  id: number; fetched_at: string; source_updated_at: string; is_current: number; accepted: number; reason: string
  text: string; article_text: string; article_title: string
}

export const researchApi = {
  digest: async (date: string, cursor = 0) => {
    const value = await readList<{ total: number; unread: number; next_cursor?: number; items: ResearchItem[]; index: { pending: number; failed: number }; coverage: SurfaceCoverage }>(`/api/research-digest?review_date=${encodeURIComponent(date)}&before_id=${cursor}`)
    if (!value.index || !value.coverage) throw new Error('研究晨报状态暂不可用，请稍后重试')
    return value
  },
  acknowledge: (id: number) => request(`/api/research-digest/${id}/acknowledge`, { method: 'POST' }),
  candidates: () => readList<{ items: TopicCandidate[]; discovery_status?: string }>('/api/theme-candidates?limit=50'),
  decide: (id: number, action: 'accept' | 'reject') => request(`/api/theme-candidates/${id}/decision`, { method: 'POST', body: JSON.stringify({ action }) }),
  createTopic: (term: string) => request('/api/research-themes', { method: 'POST', body: JSON.stringify({ term }) }),
  coverage: () => readList<SurfaceCoverage>('/api/collection/surfaces'),
  versions: (id: string) => readList<{ items: SourceVersion[] }>(`/api/posts/${encodeURIComponent(id)}/observations`),
}

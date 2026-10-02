import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ResearchDigest, TopicDiscovery } from './ResearchPanels'
import { researchApi } from './api'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
function show(component: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{component}</QueryClientProvider>)
}
it('marks research read without a stock approval and preserves edit attribution', async () => {
  vi.spyOn(researchApi, 'digest').mockResolvedValue({ total: 1, unread: 1, index: { pending: 0, failed: 0 }, coverage: { status: 'ready', gap_count: 0, target_surfaces: 3, fresh_surfaces: 3, items: [] }, items: [{ id: 7, kind: 'withdrawal', theme_name: '黄酒', detected_at: '2026-10-03T08:00:00+08:00', acknowledged_at: '', payload: { author_name: '原作者', url: 'https://example.com/post', posted_at: '2026-09-01T10:00:00+08:00', observed_at: '2026-10-03T07:00:00+08:00', source_updated_at: '', edited: true, evidence_kind: 'analysis', source_role: 'original', evidence: [{ field: 'text', text: '黄酒不再推荐' }] } }] })
  const acknowledge = vi.spyOn(researchApi, 'acknowledge').mockResolvedValue({})
  show(<ResearchDigest date="2026-10-03" />)
  expect(await screen.findByText('撤回观点 · 黄酒')).toBeInTheDocument()
  expect(screen.getByText(/源修改时间未知/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '标记已读' }))
  await waitFor(() => expect(acknowledge).toHaveBeenCalledWith(7, expect.anything()))
  expect(screen.queryByRole('button', { name: /批准/ })).not.toBeInTheDocument()
})
it('topic confirmation retains secondhand and retrospective source labels', async () => {
  vi.spyOn(researchApi, 'candidates').mockResolvedValue({ items: [{ id: 2, term: '光子计算', evidence_count: 1, evidence: [{ author_name: '作者', url: 'https://example.com/post', posted_at: '2026-10-02T12:00:00+08:00', kind: 'secondhand', source_role: 'secondhand', spans: [{ field: 'text', text: '据专家介绍，光子计算产业链进展' }] }] }] })
  vi.spyOn(researchApi, 'coverage').mockResolvedValue({ status: 'degraded', gap_count: 1, target_surfaces: 3, fresh_surfaces: 2, items: [] })
  const decide = vi.spyOn(researchApi, 'decide').mockResolvedValue({})
  show(<TopicDiscovery />)
  expect(await screen.findByText(/光子计算 · 1 篇证据/)).toBeInTheDocument()
  expect(screen.getByText(/作者.*转述.*转述/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '加入研究主题' }))
  await waitFor(() => expect(decide).toHaveBeenCalledWith(2, 'accept'))
})

it('loads older research evidence beyond the first page', async () => {
  const page = { total: 101, unread: 101, index: { pending: 0, failed: 0 }, coverage: { status: 'ready', gap_count: 0, target_surfaces: 0, fresh_surfaces: 0, items: [] } }
  const older = { id: 1, kind: 'theme_evidence', theme_name: '较早的黄酒线索', detected_at: '2026-10-03T08:00:00+08:00', acknowledged_at: '', payload: { author_name: '作者', url: '', posted_at: '', observed_at: '', source_updated_at: '', edited: false, evidence: [] } }
  const digest = vi.spyOn(researchApi, 'digest').mockImplementation(async (_date, cursor) => cursor ? { ...page, items: [older], next_cursor: 0 } : { ...page, items: [], next_cursor: 2 })
  show(<ResearchDigest date="2026-10-03" />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多研究线索' }))
  expect(await screen.findByText('主题证据 · 较早的黄酒线索')).toBeInTheDocument()
  expect(digest).toHaveBeenCalledWith('2026-10-03', 2)
  expect(screen.queryByRole('button', { name: '加载更多研究线索' })).not.toBeInTheDocument()
})

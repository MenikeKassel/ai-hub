import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { ThemeLeadsResponse } from '../types'
import ThemeRadar from './ThemeRadar'

const themeResponse: ThemeLeadsResponse = {
  summary: [{
    theme_id: 'huangjiu', theme_name: '黄酒', first_author_name: '龙头18868', first_platform: 'Zhihu',
    first_url: 'https://www.zhihu.com/question/1/answer/2', first_posted_at: '2026-09-16T11:38:28+08:00',
    first_original_posted_at: '2026-09-16T11:38:28+08:00', first_original_post_id: 'post-1',
    first_original_url: 'https://www.zhihu.com/question/1/answer/2', first_original_author_name: '龙头18868', first_original_platform: 'Zhihu',
    research_count: 1,
    first_detected_at: '2026-09-24T06:31:17+08:00', first_source_fetched_at: '2026-09-24T06:31:17+08:00',
    last_posted_at: '2026-09-28T20:21:14+08:00', source_count: 2, original_source_count: 2,
    secondhand_post_count: 1, post_count: 3, mapped_symbols: [{ symbol: '601579', name: '会稽山', instrument_type: 'stock', association: 'research_only' }],
  }],
  items: [{
    id: 1, theme_id: 'huangjiu', theme_name: '黄酒', post_id: 'post-1', kol_id: 75,
    display_name: '龙头18868', author_name: '龙头18868', handle: '18868-42', platform: 'Zhihu',
    url: 'https://www.zhihu.com/question/1/answer/2', posted_at: '2026-09-16T11:38:28+08:00',
    fetched_at: '2026-09-24T06:31:17+08:00', first_detected_at: '2026-09-24T06:31:17+08:00',
    kind: 'product', source_role: 'original', quoted_author: '', matched_terms: ['会稽山', '兰庭'],
    evidence: [
      { field: 'text', start: 0, end: 7, text: '会稽山的兰庭', matched_term: '会稽山' },
      { field: 'text', start: 8, end: 12, text: '已不再推荐', matched_term: '会稽山', context_role: 'withdrawal' },
    ],
    evidence_text: '会稽山的兰庭，第一时间买了一瓶。', claimed_timing: [],
    mapped_symbols: [{ symbol: '601579', name: '会稽山', instrument_type: 'stock', association: 'research_only' }],
    text: '只要均线低吸都是赚的。会稽山的兰庭，第一时间买了一瓶。', article_title: '', article_text: '', quoted_text: '', ocr_text: '',
    source_surface: 'answers',
    source_coverage: { status: 'bounded', historical_complete: false, requested_surfaces: ['answers', 'articles', 'ideas'], surfaces: {
      answers: { status: 'bounded' }, articles: { status: 'complete' }, ideas: { status: 'partial' },
    } },
    source_updated_at: '2026-09-17T11:38:28+08:00', post_type: 'answer', provider_warning: 'coverage:partial;surface:ideas:partial;surface:ideas:row_missing_id_text_or_question',
  }],
  total: 1, page: 1, page_size: 50, total_pages: 1, catalog_version: 'fixture',
}

function renderRadar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ThemeRadar /></QueryClientProvider>)
}

describe('theme radar', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    window.history.replaceState(null, '', '#/themes')
  })

  it('renders earliest source evidence and sends server-side sort without a blank kol_id', async () => {
    const themeLeads = vi.spyOn(api, 'themeLeads').mockResolvedValue(themeResponse)
    renderRadar()
    expect(await screen.findByRole('heading', { name: '主题雷达' })).toBeInTheDocument()
    expect(await screen.findByText(/最早命中：龙头18868/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /打开最早原创/ })).toHaveAttribute('href', themeResponse.summary[0].first_original_url)
    expect(screen.getByText('研究讨论')).toBeInTheDocument()
    expect(screen.getByText('实际首次检出')).toBeInTheDocument()
    expect(screen.getByText('原帖正文')).toBeInTheDocument()
    expect(screen.getByText('来源面：回答')).toBeInTheDocument()
    expect(screen.getByText('回答：有限分页')).toBeInTheDocument()
    expect(screen.getByText('文章：完成当前范围')).toBeInTheDocument()
    expect(screen.getByText('想法：部分缺失')).toBeInTheDocument()
    expect(screen.getByText('已不再推荐')).toBeInTheDocument()
    expect(screen.getByText('想法采集部分缺失；想法中部分记录缺少标识或正文，已保留有效记录')).toBeInTheDocument()
    expect(screen.getByText('原帖有后续编辑，当前文本不能证明主题在初次发帖时已出现')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/查看原文与字段证据/))
    expect(screen.getByText('只要均线低吸都是赚的。会稽山的兰庭，第一时间买了一瓶。')).toBeInTheDocument()
    await waitFor(() => expect(themeLeads).toHaveBeenCalled())
    const firstQuery = themeLeads.mock.calls[0][0]
    expect(firstQuery.get('sort')).toBe('newest')
    expect(firstQuery.has('kol_id')).toBe(false)

    fireEvent.change(screen.getByLabelText('主题时间顺序'), { target: { value: 'oldest' } })
    await waitFor(() => expect(themeLeads.mock.calls.at(-1)?.[0].get('sort')).toBe('oldest'))
  })

  it('keeps legacy answer-only coverage separate from the earliest original source', async () => {
    const legacyResponse: ThemeLeadsResponse = {
      ...themeResponse,
      summary: [{
        ...themeResponse.summary[0],
        first_author_name: 'NEVEN', first_platform: 'Zhihu', first_source_role: 'aggregation',
        first_url: 'https://www.zhihu.com/question/legacy/answer/1', first_posted_at: '2026-08-05T21:36:48+08:00',
        first_original_posted_at: '2026-08-13T23:35:00+08:00', first_original_post_id: 'post-2',
        first_original_url: 'https://x.com/example/status/2', first_original_author_name: '我不是糖宝', first_original_platform: 'X',
      }],
      items: [{
        ...themeResponse.items[0],
        display_name: 'NEVEN', author_name: 'NEVEN', source_role: 'aggregation', source_surface: 'answers',
        source_coverage: { status: 'legacy_answers_only', historical_complete: false, requested_surfaces: ['answers'] },
        source_updated_at: '2026-08-05T21:36:48+08:00', post_type: 'aggregation', provider_warning: 'secondhand_aggregation',
      }],
    }
    vi.spyOn(api, 'themeLeads').mockResolvedValue(legacyResponse)
    renderRadar()
    expect(await screen.findByText(/最早命中：NEVEN/)).toBeInTheDocument()
    expect(screen.getByText(/最早原创：我不是糖宝 · X/)).toBeInTheDocument()
    expect(screen.getByText('回答：有限分页')).toBeInTheDocument()
    expect(screen.getByText('文章：未请求')).toBeInTheDocument()
    expect(screen.getByText('想法：未请求')).toBeInTheDocument()
    expect(screen.getAllByText('聚合').length).toBeGreaterThan(0)
    expect(screen.getByRole('link', { name: /打开最早原创/ })).toHaveAttribute('href', 'https://x.com/example/status/2')
  })

  it('shows the local replay read time without exposing approval controls', async () => {
    vi.spyOn(api, 'themeLeads').mockResolvedValue(themeResponse)
    const extract = vi.spyOn(api, 'extractThemeLeads').mockResolvedValue({})
    renderRadar()
    fireEvent.click(await screen.findByRole('button', { name: '回放本地证据' }))
    await waitFor(() => expect(extract).toHaveBeenCalled())
    expect(await screen.findByText(/本地证据回放完成 · 读取耗时/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /批准|审批|确认/ })).not.toBeInTheDocument()
  })

  it('does not use an old authored product or recap when original discussion is absent', async () => {
    vi.spyOn(api, 'themeLeads').mockResolvedValue({ ...themeResponse, summary: [{ ...themeResponse.summary[0], first_research_posted_at: null, first_research_evidence_at: null, first_research_author_name: null, first_research_platform: null, first_research_url: null }] })
    renderRadar()
    expect(await screen.findByText('最早原创讨论：暂无')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开最早原创讨论' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /打开最早命中/ })).toHaveAttribute('href', themeResponse.summary[0].first_url)
  })
})

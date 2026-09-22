import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import { shanghaiDate } from '../workspace'
import ReviewQueue from './ReviewQueue'

function renderReviewQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ReviewQueue /></QueryClientProvider>)
}

describe('review queue scope', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    window.history.replaceState(null, '', '#/reviews')
  })

  it('canonicalizes a legacy backlog route to the current morning queue', async () => {
    window.history.replaceState(null, '', '#/reviews?scope=backlog&date=2026-01-01')
    const reviewQueue = vi.spyOn(api, 'reviewQueue').mockResolvedValue({
      review_date: shanghaiDate(),
      scope: 'morning',
      view: 'pending',
      page: 1,
      page_size: 50,
      total_posts: 0,
      total_drafts: 0,
      has_more: false,
      counts: { new: 0, processed: 0, pending: 0, failed: 0, approved: 0 },
      items: [],
      delivery: undefined,
    })

    renderReviewQueue()

    expect(await screen.findByRole('heading', { name: '今日审核' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '今日审核' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一晨报' })).toBeInTheDocument()
    expect(screen.queryByText('历史待办')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('审核日期')).not.toBeInTheDocument()
    await waitFor(() => expect(reviewQueue).toHaveBeenCalled())
    const params = reviewQueue.mock.calls.at(-1)?.[0]
    expect(params?.get('scope')).toBe('morning')
    expect(params?.get('review_date')).toBe(shanghaiDate())
    await waitFor(() => expect(window.location.hash).not.toContain('scope=backlog'))
  })
})

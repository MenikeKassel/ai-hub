import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { Kol } from '../types'
import Kols from './Kols'

const kol: Kol = {
  id: 1,
  display_name: '示例 KOL',
  platform: 'X',
  handle: 'fixture',
  profile_url: 'https://x.com/fixture',
  domain: 'A股',
  status: 'active',
  tracking_mode: 'all',
  last_post_id: '',
  last_fetched_at: '',
  last_success_at: '',
  last_gap_at: '',
  consecutive_failures: 0,
  backfill_requested: 0,
  backfill_status: 'idle',
  backfill_completed_depth: 0,
  backfill_result_count: 0,
  backfill_warning: '',
  fetch_status: 'never',
  external_account_id: 'fixture',
  availability_status: 'active',
  availability_reason: '',
  availability_checked_at: '',
}

function renderKols() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Kols /></QueryClientProvider>)
}

describe('KOL management performance loading', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('defers leaderboard loading until the performance view is selected', async () => {
    vi.spyOn(api, 'kols').mockResolvedValue([kol])
    vi.spyOn(api, 'digestAuthors').mockResolvedValue([])
    const leaderboard = vi.spyOn(api, 'kolLeaderboard').mockResolvedValue({ policy: {}, rows: [] })

    renderKols()
    expect(await screen.findByRole('heading', { name: 'KOL管理' })).toBeInTheDocument()
    await waitFor(() => expect(api.kols).toHaveBeenCalledTimes(1))
    expect(leaderboard).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '阶段表现' }))
    expect(await screen.findByRole('heading', { name: 'KOL阶段表现' })).toBeInTheDocument()
    await waitFor(() => expect(leaderboard).toHaveBeenCalledTimes(1))
  })

  it('requeues an incomplete backfill at its original target', async () => {
    const incomplete: Kol = {
      ...kol,
      backfill_status: 'needs_review',
      backfill_requested: 50,
      backfill_completed_depth: 20,
      backfill_result_count: 5,
      backfill_warning: 'provider returned no next cursor after 20 of 50 requested posts',
    }
    vi.spyOn(api, 'kols').mockResolvedValue([incomplete])
    vi.spyOn(api, 'digestAuthors').mockResolvedValue([])
    const queue = vi.spyOn(api, 'queueKolBackfill').mockResolvedValue({ ...incomplete, backfill_status: 'queued' })

    renderKols()
    expect(await screen.findByText('需复核 20/50')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新排队补抓 50 条' }))
    await waitFor(() => expect(queue).toHaveBeenCalledWith(1, 50))
  })
})

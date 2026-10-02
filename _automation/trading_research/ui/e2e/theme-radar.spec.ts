import { expect, test, type Page } from '@playwright/test'

const huangjiuSymbols = [
  { symbol: '601579', name: '会稽山', instrument_type: 'stock', association: 'research_only' },
  { symbol: '600059', name: '古越龙山', instrument_type: 'stock', association: 'research_only' },
  { symbol: '600616', name: '金枫酒业', instrument_type: 'stock', association: 'research_only' },
]

const earliestUrl = 'https://x.com/freearkshaw/status/2094982128996151421'
const summary = {
  theme_id: 'huangjiu', theme_name: '黄酒', first_author_name: '投研荟', first_platform: 'X', first_source_role: 'original', first_url: earliestUrl,
  first_posted_at: '2026-09-02T10:53:57+08:00', first_detected_at: '2026-10-02T09:00:00+08:00',
  first_source_fetched_at: '2026-09-03T19:01:58+08:00', last_posted_at: '2026-09-28T20:21:14+08:00',
  source_count: 3, original_source_count: 3, secondhand_post_count: 1, post_count: 120, mapped_symbols: huangjiuSymbols,
}

function sourceItem(overrides: Record<string, unknown> = {}) {
  return {
    id: 'theme-1', theme_id: 'huangjiu', theme_name: '黄酒', post_id: '2094982128996151421', kol_id: 58,
    display_name: '投研荟', author_name: '投研荟', handle: 'freearkshaw', platform: 'X', url: earliestUrl,
    posted_at: '2026-09-02T10:53:57+08:00', fetched_at: '2026-09-03T19:01:58+08:00',
    first_detected_at: '2026-10-02T09:00:00+08:00', kind: 'analysis', source_role: 'original', quoted_author: '',
    matched_terms: ['黄酒'], evidence: [{ field: 'text', start: 16, end: 18, text: '开始吹低度的黄酒', matched_term: '黄酒' }],
    evidence_text: '现在市场也不大吹白酒了，开始吹低度的黄酒。', claimed_timing: [], mapped_symbols: huangjiuSymbols,
    text: '现在市场也不大吹白酒了，开始吹低度的黄酒。', article_title: '', article_text: '', quoted_text: '', ocr_text: '',
    ...overrides,
  }
}

const retrospective = sourceItem({
  id: 'theme-retro', post_id: '2084791848351052340', kol_id: 75, display_name: '龙头18868', author_name: '龙头18868',
  handle: '18868-42', platform: 'Zhihu', url: 'https://www.zhihu.com/question/524777137/answer/2084791848351052340',
  posted_at: '2026-09-19T23:51:50+08:00', fetched_at: '2026-09-27T19:27:05+08:00',
  first_detected_at: '2026-10-02T09:05:00+08:00', kind: 'retrospective', claimed_timing: ['9月初'],
  matched_terms: ['黄酒', '兰庭'], evidence_text: '我于九月初反复提示过黄酒机会，并回顾购买了兰庭产品。',
  text: '我于九月初反复提示过黄酒机会，并回顾购买了兰庭产品。',
})

const quoted = sourceItem({
  id: 'theme-quoted', post_id: 'quoted-1', kol_id: 90, display_name: '收藏聚合', author_name: '郑国成',
  handle: 'saved-archive', platform: 'X', url: 'https://x.com/archive/status/quoted-1', source_role: 'quoted',
  quoted_author: '郑国成', kind: 'secondhand', matched_terms: ['黄酒'],
  evidence: [
    { field: 'article_title', start: 0, end: 8, text: '会稽山怎么看？', matched_term: '会稽山', source_role: 'quoted', kind: 'secondhand', context_role: 'question' },
    { field: 'quoted_text', start: 0, end: 4, text: '郑国成提到黄酒', matched_term: '黄酒', source_role: 'quoted', kind: 'secondhand', context_role: 'quoted_text' },
  ],
  evidence_text: '郑国成提到黄酒。', article_title: '会稽山怎么看？', quoted_text: '郑国成提到黄酒。', text: '',
})

const newest = sourceItem({
  id: 'theme-newest', post_id: '2088000342046794367', kol_id: 75, display_name: '龙头18868', author_name: '龙头18868',
  handle: '18868-42', platform: 'Zhihu', url: 'https://www.zhihu.com/question/1/answer/2088000342046794367',
  posted_at: '2026-09-28T20:21:14+08:00', first_detected_at: '2026-10-02T09:10:00+08:00', kind: 'recommendation',
  evidence_text: '资金涌向会稽山。', text: '资金涌向会稽山。', matched_terms: ['会稽山'],
})

function pageItems(page: number, sort: string, query: string) {
  if (query === '郑国成') return []
  if (query === '会稽山') return [newest]
  if (sort === 'oldest' && page === 1) return [retrospective, sourceItem()]
  if (sort === 'oldest') return [sourceItem({ id: `theme-page-${page}`, post_id: `theme-page-${page}`, posted_at: '2026-09-03T10:00:00+08:00' })]
  return [newest, quoted, retrospective, ...Array.from({ length: 47 }, (_, index) => sourceItem({ id: `theme-${index + 4}`, post_id: `theme-${index + 4}`, posted_at: `2026-09-${String(18 - (index % 9)).padStart(2, '0')}T10:00:00+08:00` }))]
}

async function mockApi(page: Page, requests: URL[]) {
  await page.route('**/api/**', async (route) => {
    const requestUrl = new URL(route.request().url())
    const path = requestUrl.pathname
    if (path === '/api/theme-leads') {
      requests.push(requestUrl)
      const query = requestUrl.searchParams.get('q') || ''
      const pageNumber = Number(requestUrl.searchParams.get('page') || 1)
      const sort = requestUrl.searchParams.get('sort') || 'newest'
      if (query === '郑国成') {
        await route.fulfill({ json: { summary: [], items: [], total: 0, page: pageNumber, page_size: 50, total_pages: 0, catalog_version: 'fixture' } })
        return
      }
      const isSearch = query === '会稽山'
      await route.fulfill({ json: {
        summary: isSearch ? [summary] : [summary],
        items: isSearch ? pageItems(1, sort, query) : pageItems(pageNumber, sort, query),
        total: isSearch ? 1 : 120, page: pageNumber, page_size: 50, total_pages: isSearch ? 1 : 3, catalog_version: 'fixture',
      } })
      return
    }
    if (path === '/api/theme-leads/extract' && route.request().method() === 'POST') {
      await route.fulfill({ json: { processed_posts: 120, created: 0, updated: 0, removed: 0, failed: 0, catalog_version: 'fixture' } })
      return
    }
    if (path === '/api/pipeline/status') {
      await route.fulfill({ json: {
        latest_fetch_at: '2026-10-02T09:00:00+08:00', latest_fetch_status: 'success', latest_ai_at: '', pending_ai: 0,
        failed_ai: 0, pending_human_review: 0, queue_review_date: '2026-10-02', queue_kind: 'today', morning_delivery: null,
        next_preview: {}, latest_trade_date: '2026-10-01', expected_trade_date: '2026-10-01', market_status: 'closed', lagging_symbols: [],
      } })
      return
    }
    await route.fulfill({ json: [] })
  })
}

test('主题雷达服务端排序、分页与来源角色保持可核对', async ({ page }) => {
  const requests: URL[] = []
  await mockApi(page, requests)
  await page.goto('/#/themes')

  await expect(page.getByRole('heading', { name: '主题雷达', exact: true })).toBeVisible()
  await expect(page.getByText('最早命中：投研荟 · X')).toBeVisible()
  await expect(page.getByRole('link', { name: /打开最早命中/ })).toHaveAttribute('href', earliestUrl)
  await expect(page.getByText('第 1 / 3 页', { exact: true })).toBeVisible()
  await expect(page.locator('.theme-source-item.role-quoted .badge').first()).toHaveText('引用')
  await expect(page.locator('.theme-source-item.role-quoted')).toContainText('实际原作者：郑国成')
  await page.locator('.theme-source-item.role-quoted').getByText(/查看原文与字段证据/).click()
  await expect(page.locator('.theme-source-item.role-quoted')).toContainText('问题标题（不是回答作者观点）')

  await page.getByLabel('主题时间顺序').selectOption('oldest')
  await expect.poll(() => requests.at(-1)?.searchParams.get('sort')).toBe('oldest')
  await expect(page.locator('.theme-source-item').filter({ hasText: '9月初' }).getByText('回顾', { exact: true })).toBeVisible()
  await expect(page.locator('.theme-source-item').filter({ hasText: '9月初' })).not.toContainText('推荐')
  await page.getByRole('button', { name: '下一页' }).click()
  await expect.poll(() => requests.at(-1)?.searchParams.get('page')).toBe('2')
  await expect.poll(() => requests.at(-1)?.searchParams.get('sort')).toBe('oldest')

  await page.goto('/#/themes?q=%E4%BC%9A%E7%A8%BD%E5%B1%B1')
  await expect(page.getByText('当前返回 1 条证据')).toBeVisible()
  await expect(page.locator('.theme-source-item')).toHaveCount(1)
  await expect(page.locator('.theme-source-item').getByText('601579 会稽山 · 研究关联')).toBeVisible()
  await expect(page.locator('.theme-radar-workspace').getByText('股票提及')).toHaveCount(0)
})

test('主题雷达在无郑国成命中时保留覆盖边界提示并适配窄屏', async ({ page }) => {
  const requests: URL[] = []
  await mockApi(page, requests)
  await page.goto('/#/themes?q=%E9%83%91%E5%9B%BD%E6%88%90')
  await expect(page.getByText('没有词典命中的库内证据')).toBeVisible()
  await expect(page.getByText(/知乎采集范围存在缺口/)).toBeVisible()
  await expect(page.getByText(/当前仅检索已配置的研究主题/)).toBeVisible()
  await expect.poll(() => requests.at(-1)?.searchParams.get('q')).toBe('郑国成')
  await expect(page.getByRole('button', { name: /批准|审批|确认/ })).toHaveCount(0)
  expect(await page.evaluate(() => document.body.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
})

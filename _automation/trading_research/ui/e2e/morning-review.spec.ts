import { expect, test } from '@playwright/test'

const draft = {
  id: 1,
  post_id: 'fixture-post-1',
  symbol: '603757',
  security_name: '大元泵业',
  direction: 'long',
  thesis: '液冷赛道景气，趋势向上',
  evidence_type: 'original_pre_event',
  evidence_spans: ['大元泵业：液冷赛道景气，趋势向上'],
  conditions: ['今日建仓计划'],
  mention_kind: 'recommendation',
  confidence: 0.99,
  model_name: 'codex-batch',
  extraction_version: 'fixture-v1',
  source_signature: 'fixture-signature',
  status: 'ready',
  attention_reasons: [],
  queue_scope: 'morning',
  review_date: '2026-07-17',
  review_note: '',
  event_id: '',
  reviewed_at: '',
  created_at: '2026-07-17T08:30:00+08:00',
  updated_at: '2026-07-17T08:30:00+08:00',
  url: 'https://x.com/fixture/status/1',
  text: '今日建仓计划：大元泵业、沃顿科技\n\n大元泵业：液冷赛道景气，趋势向上',
  article_title: '',
  article_text: '',
  quoted_text: '',
  posted_at: '2026-07-17T08:20:00+08:00',
  post_type: 'original',
  reply_to_id: '',
  provider_warning: '',
  review_status: 'pending',
  display_name: '演示研究员',
  handle: 'fixture',
  local_media: [],
}

const post = {
  ...draft,
  kol_id: 1,
  author_name: '演示研究员',
  domain: 'A股',
  media: [],
  metrics: {},
  reply_to_id: '',
  reply_to_author: '',
  quoted_author: '',
  review_note: '',
  rule_score: 10,
  rule_reasons: ['fixture'],
  rule_symbols: ['603757'],
  rule_direction: 'long',
  content_type: 'recommendation',
  is_candidate: true,
  model_status: 'completed',
  model_summary: '提取到大元泵业推荐',
  model_error: '',
  ocr_status: 'not_needed',
  ocr_attempts: 0,
  ocr_text: '',
  ocr_error: '',
  ocr_updated_at: '',
  source_note: '',
  notion_url: '',
  canonical_provider: 'fixture',
  metrics_provider: 'fixture',
}

const attentionDraft = {
  ...draft,
  id: 2,
  status: 'needs_attention',
  attention_reasons: ['evidence_not_found'],
  evidence_spans: ['建议关注大元泵业'],
  evidence_source: 'ocr',
  depends_on_ocr: true,
  ocr_text: '大盘环境较差，锚定低吸大元泵业',
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/pipeline/status', async (route) => route.fulfill({ json: { lagging_symbols: [], refresh: {} } }))
  await page.route('**/api/morning-review?**', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        review_date: '2026-07-17',
        summary: {
          new_posts: 1,
          ai_processed: 1,
          waiting_review: 1,
          ai_failed: 0,
          approved_today: 0,
        },
        posts: [post],
        drafts: [draft],
        approved_drafts: [],
      }),
    })
  })
})

test('renders the per-stock morning review without viewport overflow', async ({ page }, testInfo) => {
  await page.goto('/#/reviews')

  await expect(page.getByRole('heading', { name: '今日审核' })).toBeVisible()
  await expect(page.getByText('待你确认').locator('..').getByText('1')).toBeVisible()
  await expect(page.getByRole('textbox', { name: '推荐理由' })).toHaveValue('液冷赛道景气，趋势向上')
  await expect(page.getByRole('button', { name: '批准并跟踪' })).toBeVisible()
  await page.getByRole('button', { name: /新增帖子\s*1/ }).click()
  await expect(page.getByText('当天采集到的全部原帖，包括尚未完成 AI 处理的内容。')).toBeVisible()
  await page.getByRole('button', { name: /待你确认\s*1/ }).click()
  await expect(page.locator('.sidebar .nav-item:visible, .mobile-nav button:visible')).toHaveCount(8)

  const horizontalOverflow = await page.evaluate(() => document.body.scrollWidth - window.innerWidth)
  expect(horizontalOverflow).toBeLessThanOrEqual(1)

  const approve = page.getByRole('button', { name: '批准并跟踪' })
  await approve.scrollIntoViewIfNeeded()
  const box = await approve.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width)

  await page.screenshot({ path: testInfo.outputPath('morning-review.png'), fullPage: true })
})

test('requires an exact source quote before an attention draft can be approved', async ({ page }) => {
  await page.unroute('**/api/morning-review?**')
  await page.route('**/api/morning-review?**', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        review_date: '2026-07-17',
        summary: { new_posts: 1, ai_processed: 1, waiting_review: 1, ai_failed: 0, approved_today: 0 },
        posts: [{ ...post, ocr_text: attentionDraft.ocr_text }],
        drafts: [attentionDraft],
        approved_drafts: [],
      }),
    })
  })
  await page.goto('/#/reviews')

  await expect(page.locator('.ocr-source')).toBeVisible()
  await expect(page.getByRole('button', { name: '先修复证据' })).toBeDisabled()
  await page.getByLabel('原文证据').fill(attentionDraft.ocr_text)
  await page.getByLabel('AI 错误类型').selectOption('wrong_evidence')
  await expect(page.getByRole('button', { name: '保存并批准跟踪' })).toBeEnabled()
  expect(await page.evaluate(() => document.body.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
})

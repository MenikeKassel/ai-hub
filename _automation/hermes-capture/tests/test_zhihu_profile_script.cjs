// Run with Node after installing the console UI dependencies. These tests
// execute the browser fetch script, including HTTP-200 API error responses.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { JSDOM } = require(path.resolve(__dirname, '../../trading_research/ui/node_modules/jsdom'))

const source = fs.readFileSync(path.resolve(__dirname, '../zhihu_profile_capture.py'), 'utf8')
const rawScript = source.match(/PROFILE_FETCH_SCRIPT = r"""([\s\S]*?)"""/)[1]

function row(surface, id = '90001') {
  return {
    id, author: { name: 'Fixture author', url_token: 'fixture' },
    created_time: 1788400000, question: { id: '80001', title: 'Question context' },
    title: 'Article context', content: surface === 'pins'
      ? [{ type: 'text', content: '<p>Yellow rice wine industry evidence</p>' }]
      : '<p>Yellow rice wine industry evidence</p>',
  }
}

async function run(fetchResponse, surfaces = ['answers', 'articles', 'ideas'], limit = 2) {
  const dom = new JSDOM('', { runScripts: 'outside-only', url: 'https://www.zhihu.com' })
  const calls = []
  dom.window.setTimeout = (callback, delay) => setTimeout(callback, Math.min(delay, 5))
  dom.window.clearTimeout = clearTimeout
  dom.window.fetch = async (url) => {
    calls.push(url)
    const parsed = new URL(url, 'https://www.zhihu.com')
    const surface = parsed.pathname.split('/').pop()
    const value = await fetchResponse(surface, Number(parsed.searchParams.get('offset')), calls)
    return { ok: (value.status || 200) < 400, status: value.status || 200,
      text: async () => value.raw === undefined ? JSON.stringify(value.body) : value.raw }
  }
  const replacements = { __HANDLE__: JSON.stringify('fixture'), __SURFACES__: JSON.stringify(surfaces),
    __LIMIT__: String(limit), __MAX_CHARS__: '5000', __BUDGET_MS__: '90000', __REQUEST_TIMEOUT_MS__: '1000' }
  let script = rawScript
  for (const [key, value] of Object.entries(replacements)) script = script.replaceAll(key, value)
  try { return { value: JSON.parse(JSON.stringify(await dom.window.eval(script))), calls } }
  finally { dom.window.close() }
}

test('all three surfaces retain text, identity, and real source time', async () => {
  const { value } = await run(async (surface) => ({ body: { data: [row(surface)], paging: { is_end: true } } }))
  assert.equal(value.coverage.status, 'complete')
  assert.equal(value.coverage.historical_complete, false)
  assert.equal(value.posts.length, 3)
  assert.deepEqual(value.posts.map((post) => post.type), ['answer', 'article', 'idea'])
  assert.ok(value.posts.every((post) => post.text && post.createdAtISO && post.author.screenName === 'fixture'))
})

test('HTTP-200 authentication errors remain failed while other surfaces survive', async () => {
  const { value, calls } = await run(async (surface) => ({ body: surface === 'articles'
    ? { error: { code: 4041, message: 'Authentication required' } }
    : { data: [row(surface)], paging: { is_end: true } } }))
  assert.equal(value.coverage.status, 'partial')
  assert.equal(value.posts.length, 2)
  assert.equal(value.surfaces.articles.status, 'failed')
  assert.equal(calls.filter((url) => url.includes('/articles?')).length, 1)
})

test('10003 has three bounded attempts and never becomes an empty success', async () => {
  const { value, calls } = await run(async () => ({ body: { error: { code: 10003 } } }), ['answers'])
  assert.equal(value.ok, false)
  assert.equal(value.coverage.status, 'failed')
  assert.equal(calls.length, 3)
  assert.equal(value.posts.length, 0)
})

test('a second page failure preserves first-page evidence and partial coverage', async () => {
  const { value } = await run(async (surface, offset) => offset
    ? { status: 403, body: {} }
    : { body: { data: [row(surface)], paging: { is_end: false } } }, ['answers'])
  assert.equal(value.coverage.status, 'partial')
  assert.equal(value.posts.length, 1)
})

test('pagination limit and repeated pages cannot claim historical completeness', async () => {
  const { value, calls } = await run(async (surface) => ({ body: {
    data: [row(surface)], paging: { is_end: false },
  } }), ['answers'], 3)
  assert.notEqual(value.coverage.status, 'complete')
  assert.equal(value.coverage.historical_complete, false)
  assert.equal(value.posts.length, 1)
  assert.ok(calls.length <= 3)
})

test('a genuine empty feed is distinct from malformed JSON', async () => {
  const empty = await run(async () => ({ body: { data: [], paging: { is_end: true } } }), ['answers'])
  assert.equal(empty.value.ok, true)
  assert.equal(empty.value.surfaces.answers.status, 'empty')
  const invalid = await run(async () => ({ raw: '<html>Login</html>' }), ['answers'])
  assert.equal(invalid.value.ok, false)
  assert.equal(invalid.value.surfaces.answers.status, 'failed')
})

import { useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatDate } from '../../api'
import QueryState from '../../components/QueryState'
import { researchApi } from './api'

const kinds: Record<string, string> = { theme_evidence: '主题证据', viewpoint_change: '观点变化', withdrawal: '撤回观点', evidence_removed: '原文删除主题', new_topic: '新主题待核对', coverage_gap: '采集缺口' }
const roles: Record<string, string> = { prospective: '前瞻', recommendation: '明确观点', analysis: '分析', retrospective: '回顾', product: '产品体验', secondhand: '转述', original: '原创', quoted: '引用', aggregation: '汇编' }
const surfaces: Record<string, string> = { answers: '回答', articles: '文章', ideas: '想法', timeline: '时间线' }
const statuses: Record<string, string> = { bounded: '限量采集', range_complete: '本次范围结束', partial: '部分成功', failed: '失败', never: '未采集', unknown: '未验证' }

export function ResearchDigest({ date }: { date: string }) {
  const client = useQueryClient()
  const query = useInfiniteQuery({ queryKey: ['research-digest', date], initialPageParam: 0, queryFn: ({ pageParam }) => researchApi.digest(date, pageParam), getNextPageParam: (page) => page.next_cursor || undefined, refetchInterval: 15_000 })
  const latest = query.data?.pages[0]
  const items = query.data?.pages.flatMap((page) => page.items)
  const acknowledge = useMutation({ mutationFn: researchApi.acknowledge, onSuccess: () => client.invalidateQueries({ queryKey: ['research-digest'] }) })
  return <details className="panel research-panel">
    <summary>主题研究晨报 · {latest ? `${latest.unread} 条未读 / ${latest.total} 条` : '正在读取'}</summary>
    <p>原文主题、观点变化与新主题线索。已读仅标记阅读状态。</p>
    <QueryState loading={query.isLoading} error={query.error || acknowledge.error} stale={Boolean(query.data)} />
    {latest && <p>索引等待 {latest.index.pending} 条 · 重试 {latest.index.failed} 条 · 来源面缺口 {latest.coverage.gap_count} 项</p>}
    <div className="research-items">{items?.map((item) => <article key={item.id} className="research-item">
      <div><strong>{kinds[item.kind] || item.kind} · {item.theme_name}</strong><span className="badge neutral">{roles[item.payload.evidence_kind || ''] || item.payload.evidence_kind} · {roles[item.payload.source_role || ''] || item.payload.source_role}</span></div>
      <p>{item.payload.author_name} · 原帖 {formatDate(item.payload.posted_at)} · 检出 {formatDate(item.detected_at)}</p>
      {item.payload.resolved_at && <p>后续采集已恢复 · {formatDate(item.payload.resolved_at)}；保留当次缺口记录。</p>}
      {item.payload.edited && <p className="notice-banner">后续版本变化 · 观察 {formatDate(item.payload.observed_at)}{item.payload.source_updated_at ? ` · 源修改 ${formatDate(item.payload.source_updated_at)}` : ' · 源修改时间未知'}</p>}
      {item.payload.evidence?.slice(0, 3).map((span, i) => <blockquote key={i}>{span.text}</blockquote>)}
      <div className="header-actions">{item.payload.url && <a href={item.payload.url} target="_blank" rel="noreferrer">核对原帖</a>}<button className="secondary-button" disabled={Boolean(item.acknowledged_at) || acknowledge.isPending} onClick={() => acknowledge.mutate(item.id)}>{item.acknowledged_at ? '已读' : '标记已读'}</button></div>
    </article>)}</div>
    {query.hasNextPage && <button className="secondary-button" disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>{query.isFetchingNextPage ? '正在读取' : '加载更多研究线索'}</button>}
    {latest?.total === 0 && <p>当前晨报没有新主题证据。历史回放保留在主题雷达。</p>}
  </details>
}

export function TopicDiscovery() {
  const client = useQueryClient()
  const [term, setTerm] = useState('')
  const create = useMutation({ mutationFn: researchApi.createTopic, onSuccess: () => { setTerm(''); client.invalidateQueries({ queryKey: ['theme-leads'] }) } })
  const topics = useQuery({ queryKey: ['theme-candidates'], queryFn: researchApi.candidates, refetchInterval: 30_000 })
  const coverage = useQuery({ queryKey: ['surface-coverage'], queryFn: researchApi.coverage, refetchInterval: 30_000 })
  const decision = useMutation({ mutationFn: ({ id, action }: { id: number; action: 'accept' | 'reject' }) => researchApi.decide(id, action), onSuccess: () => {
    client.invalidateQueries({ queryKey: ['theme-candidates'] }); client.invalidateQueries({ queryKey: ['theme-leads'] })
  } })
  return <div className="research-panel-group">
    <details className="panel research-panel"><summary>开放主题发现 · {topics.data?.items.length ?? '—'} 条待核对线索</summary>
      <p>核对原文后加入研究主题，确认后自动回放本地库存。当前按证据数量展示 50 条；处理后继续显示其余线索。</p>
      {topics.data?.discovery_status === 'degraded' && <p className="notice-banner">主题分词组件未就绪，自动中文主题发现暂停；已登记主题继续索引。</p>}
      <form className="header-actions" onSubmit={(event) => { event.preventDefault(); if (term.trim()) create.mutate(term.trim()) }}><input aria-label="添加研究主题" placeholder="输入需持续研究的板块词" value={term} maxLength={24} onChange={(event) => setTerm(event.target.value)} /><button className="secondary-button" disabled={create.isPending || term.trim().length < 2}>添加研究主题</button></form>
      <QueryState loading={topics.isLoading} error={topics.error || decision.error || create.error} stale={Boolean(topics.data)} />
      <div className="research-items">{topics.data?.items.map((topic) => <article key={topic.id} className="research-item"><strong>{topic.term} · {topic.evidence_count} 篇证据</strong>
        {topic.evidence.map((e, i) => <div key={i}><p>{e.author_name} · {formatDate(e.posted_at)} · {roles[e.kind] || e.kind} · {roles[e.source_role] || e.source_role}</p><blockquote>{e.spans[0]?.text}</blockquote><a href={e.url} target="_blank" rel="noreferrer">核对原帖</a></div>)}
        <div className="header-actions"><button className="secondary-button" disabled={decision.isPending} onClick={() => decision.mutate({ id: topic.id, action: 'accept' })}>加入研究主题</button><button className="secondary-button" disabled={decision.isPending} onClick={() => decision.mutate({ id: topic.id, action: 'reject' })}>排除线索</button></div>
      </article>)}</div>
    </details>
    <details className="panel research-panel"><summary>逐账号采集覆盖 · {coverage.data?.gap_count ?? '—'} 项缺口</summary><p>回答、文章、想法分别记账。限量采集或空结果不能证明历史完整。</p>
      <QueryState loading={coverage.isLoading} error={coverage.error} stale={Boolean(coverage.data)} />
      <div className="research-items">{coverage.data?.items.map((item) => <p key={`${item.kol_id}-${item.surface}`}>{item.display_name || item.handle} · {surfaces[item.surface] || item.surface} · {statuses[item.status] || item.status} · {item.fresh ? '近期已采' : '需要更新'} · {item.origin === 'legacy_snapshot' ? '条数未保留' : item.origin === 'unknown' ? '条数未知' : `${item.received_count} 条`} · {formatDate(item.recorded_at)}{item.origin === 'legacy_snapshot' ? '（历史快照恢复）' : ''}</p>)}</div>
    </details>
  </div>
}

export function SourceVersions({ postId }: { postId: string }) {
  const [open, setOpen] = useState(false)
  const query = useQuery({ queryKey: ['source-versions', postId], queryFn: () => researchApi.versions(postId), enabled: open })
  return <div className="source-versions"><button className="secondary-button" onClick={() => setOpen(!open)}>{open ? '收起原文版本' : '核对原文版本'}</button>{open && <>
    <p>最初原帖用于审批；主题证据使用经来源校验的最新版本。后编辑内容不能证明首次发表时已存在。</p>
    <QueryState loading={query.isLoading} error={query.error} stale={Boolean(query.data)} />
    {query.data?.items.map((version) => <article className="research-item" key={version.id}><p>{version.is_current ? '当前研究版本' : version.accepted ? '历史版本' : '未采信版本'} · 采集 {formatDate(version.fetched_at)} · 源修改 {version.source_updated_at ? formatDate(version.source_updated_at) : '未知'}</p>{version.article_title && <strong>{version.article_title}</strong>}{version.text && <p className="theme-original-text">{version.text}</p>}{version.article_text && version.article_text !== version.text && <p className="theme-original-text">{version.article_text}</p>}</article>)}
    {query.data?.items.length === 0 && <p>暂无后续版本，保留初始原帖。</p>}
  </>}</div>
}

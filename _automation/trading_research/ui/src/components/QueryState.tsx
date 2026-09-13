export default function QueryState({ loading, error, stale }: { loading?: boolean; error?: Error | null; stale?: boolean }) {
  if (error) return <div className="error-banner" role="alert">{stale ? '更新失败，保留上次读取的数据。' : '读取失败。'}{error.message}</div>
  if (loading) return <div className="loading" role="status">正在读取…</div>
  return null
}

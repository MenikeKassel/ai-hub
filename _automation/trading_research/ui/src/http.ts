export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg?: string }) => item.msg || response.statusText).join('；') : body.detail
    throw new ApiError(typeof detail === 'string' ? detail : response.statusText, response.status)
  }
  return response.json() as Promise<T>
}

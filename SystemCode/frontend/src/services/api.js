import { auth } from '../state/auth'

const apiBase = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-User-ID': auth.user?.id || 'anonymous', ...(options.headers || {}) }
  })
  if (!response.ok) {
    let detail
    try { detail = await response.json() } catch { detail = {} }
    throw new Error(detail?.detail?.message || detail?.detail?.code || detail?.detail || `Request failed (${response.status})`)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  createSession: () => request('/api/v1/sessions', { method: 'POST', body: JSON.stringify({ locale: 'en-SG', market: 'SG', currency: 'SGD', long_term_memory: true }) }),
  getSession: (id) => request(`/api/v1/sessions/${id}`),
  sendMessage: (id, version, text) => request(`/api/v1/sessions/${id}/messages`, { method: 'POST', body: JSON.stringify({ client_message_id: crypto.randomUUID(), expected_requirements_version: version, text }) }),
  createRun: (id, version, mode = 'dag') => request(`/api/v1/sessions/${id}/runs`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ requirements_version: version, orchestration_mode: mode, maximum_options: 3 }) }),
  getRun: (id) => request(`/api/v1/runs/${id}`),
  getResult: (id) => request(`/api/v1/runs/${id}/result`),
  getExecution: (id) => request(`/api/v1/runs/${id}/execution`),
  prices: (params = '') => request(`/data/prices?${params}`),
  memories: (sessionId) => request(`/api/v1/sessions/${sessionId}/memories`),
  addMemory: (sessionId, kind, value) => request(`/api/v1/sessions/${sessionId}/memories?kind=${encodeURIComponent(kind)}&value=${encodeURIComponent(value)}&confirmed=true`, { method: 'POST' }),
  deleteMemory: (id) => request(`/api/v1/memories/${id}`, { method: 'DELETE' }),
  health: () => request('/api/v1/health')
}

import api from './api'
import { SSEParser, type SSEEvent } from './sseParser'

const API_BASE_URL = (typeof import.meta !== 'undefined' &&
  (import.meta as any).env?.VITE_API_BASE_URL) || '/api/v1'

export interface IssueDiagnosisRequest {
  data_source_type: 'pr_pipeline' | 'ci_job' | 'commit' | 'manual'
  pr_number?: number
  job_id?: number
  run_id?: number
  commit_sha?: string
  user_prompt?: string
  conversation_history?: DiagnosisMessage[]
}

export interface DiagnosisMessage {
  role: 'user' | 'assistant'
  content: string
}

export function formatApiError(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map(item => {
        if (typeof item !== 'object' || item === null || !('msg' in item)) return ''
        return typeof item.msg === 'string' ? item.msg : ''
      })
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  return fallback
}

export interface CIJobOption {
  job_id: number
  run_id: number
  workflow_name: string
  job_name: string
  conclusion: string
  completed_at: string | null
}

export interface CommitOption {
  sha: string
  message: string
  committed_at: string | null
  run_id: number | null
  run_number: number | null
}

export const getFailedCIJobs = async (daysBack: number = 7): Promise<CIJobOption[]> => {
  const token = localStorage.getItem('access_token')
  const response = await fetch(
    `${API_BASE_URL}/issue-diagnosis/data-sources/ci-jobs?days_back=${daysBack}`,
    {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    }
  )
  if (!response.ok) {
    throw new Error(`Failed to fetch CI jobs: ${response.statusText}`)
  }
  return response.json()
}

export const getRecentCommits = async (daysBack: number = 7): Promise<CommitOption[]> => {
  const token = localStorage.getItem('access_token')
  const response = await fetch(
    `${API_BASE_URL}/issue-diagnosis/data-sources/commits?days_back=${daysBack}`,
    {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    }
  )
  if (!response.ok) {
    throw new Error(`Failed to fetch commits: ${response.statusText}`)
  }
  return response.json()
}

export const streamDiagnosis = async (
  request: IssueDiagnosisRequest,
  onChunk: (content: string) => void,
  onMeta: (meta: { provider: string; model: string }) => void,
  onDone: (summary: { total_content_length: number; duration_seconds: number; chunk_count: number }) => void,
  onError: (message: string) => void,
  signal?: AbortSignal,
): Promise<void> => {
  const token = localStorage.getItem('access_token')

  const response = await fetch(
    `${API_BASE_URL}/issue-diagnosis/diagnose`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
      signal,
    }
  )

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: response.statusText }))
    onError(formatApiError(errorData.detail, response.statusText || '诊断请求失败'))
    return
  }

  const reader = response.body?.getReader()
  if (!reader) {
    onError('No response body')
    return
  }

  const decoder = new TextDecoder()
  const parser = new SSEParser()
  let terminated = false

  const handleEvent = (event: SSEEvent) => {
    if (terminated) return
    try {
      const data = JSON.parse(event.data)
      if (event.event === 'chunk') {
        onChunk(data.content)
      } else if (event.event === 'meta') {
        onMeta(data)
      } else if (event.event === 'done') {
        terminated = true
        onDone(data)
      } else if (event.event === 'error') {
        terminated = true
        onError(data.message)
      }
    } catch (error) {
      terminated = true
      console.warn('SSE data parse error:', error)
      onError('AI 流式响应格式错误，请重试')
      void reader.cancel()
    }
  }

  let reading = true
  while (reading) {
    const { done, value } = await reader.read()
    if (done) {
      reading = false
      continue
    }
    parser.push(decoder.decode(value, { stream: true })).forEach(handleEvent)
  }

  parser.finish(decoder.decode()).forEach(handleEvent)
  if (!terminated) onError('AI 流式响应意外中断，请重试')
}

export interface DiagnosisHistoryItem {
  id: number
  username: string
  diagnosis_type: string
  target_id: string
  target_label: string
  model_used: string
  duration_seconds: number
  status: string
  is_liked: boolean
  like_count: number
  report_preview: string
  created_at: string
}

export interface DiagnosisHistoryResponse {
  total: number
  page: number
  page_size: number
  items: DiagnosisHistoryItem[]
}

export interface DiagnosisStats {
  total: number
  success_count: number
  success_rate: number
  liked_count: number
  pr_pipeline_count: number
  ci_job_count: number
}

export interface DiagnosisDetail extends DiagnosisHistoryItem {
  report_content: string
}

export async function getDiagnosisHistory(params: { page?: number; page_size?: number; diagnosis_type?: string; liked_only?: boolean }): Promise<DiagnosisHistoryResponse> {
  const searchParams = new URLSearchParams()
  if (params.page) searchParams.set('page', String(params.page))
  if (params.page_size) searchParams.set('page_size', String(params.page_size))
  if (params.diagnosis_type) searchParams.set('diagnosis_type', params.diagnosis_type)
  if (params.liked_only) searchParams.set('liked_only', 'true')
  const { data } = await api.get(`/issue-diagnosis/history?${searchParams.toString()}`)
  return data
}

export async function getDiagnosisStats(): Promise<DiagnosisStats> {
  const { data } = await api.get('/issue-diagnosis/history/stats')
  return data
}

export async function getDiagnosisDetail(id: number): Promise<DiagnosisDetail> {
  const { data } = await api.get(`/issue-diagnosis/history/${id}`)
  return data
}

export async function toggleDiagnosisLike(id: number): Promise<{ id: number; is_liked: boolean; like_count: number }> {
  const { data } = await api.post(`/issue-diagnosis/history/${id}/like`)
  return data
}

export async function saveDiagnosisRecord(params: {
  diagnosis_type: string
  target_id: string
  target_label?: string
  report_content: string
  model_used?: string
  duration_seconds?: number
  status?: string
}): Promise<{ id: number; status: string }> {
  const { data } = await api.post('/issue-diagnosis/history', params)
  return data
}

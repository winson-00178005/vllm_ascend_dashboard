/**
 * 每日总结 API 服务
 */
import apiClient, { longTimeoutApiClient } from './api'

export interface DailySummary {
  project: string
  date: string
  summary_markdown: string
  has_data: boolean
  pr_count: number
  issue_count: number
  commit_count: number
  generated_at: string | null
  status?: string  // success, failed, not_generated
}

export interface DailySummaryListItem {
  date: string
  project: string
  pr_count: number
  issue_count: number
  commit_count: number
  has_data: boolean
  generated_at: string
}

export interface GenerateSummaryParams {
  project: string
  date?: string
  llm_provider?: string
  force_regenerate?: boolean
}

export interface FetchDataParams {
  project: string
  date: string
  force_refresh?: boolean
}

export interface LLMProvider {
  provider: string
  display_name: string
  default_model: string
  is_active: boolean  // 是否为当前激活的提供商（用于 AI 总结）
  display_order: number
  api_key_configured: boolean
  api_key_preview?: string  // API Key 预览（前 8 位 + ... + 后 4 位）
  api_base_url?: string
}

export type SystemPromptScope = 'daily_summary' | 'commit_analysis' | 'ci_failure_analysis'

export interface SystemPromptConfig {
  prompts: {
    ascend?: string
    vllm?: string
    default?: string
  } & Record<string, string>
  description: string
}

export interface GitHubUser {
  login: string
  avatar_url?: string
  html_url?: string
}

export type GitHubActor = string | GitHubUser | null

export interface DailyDataItem {
  number: number
  title: string
  state: string
  user: GitHubActor
  html_url: string
  created_at: string | null
  merged_at?: string | null  // PR 合入时间（仅 PR 类型有）
  labels?: Array<{ name: string; color: string; description?: string }>
  body?: string
}

export interface DailyCommitItem {
  sha: string
  message: string
  author: GitHubActor
  html_url: string
  committed_at: string | null
  pr_number?: number
  pr_title?: string
  additions?: number
  deletions?: number
}

export interface DailyDataResponse {
  project: string
  date: string
  pull_requests: DailyDataItem[]
  issues: DailyDataItem[]
  commits: DailyCommitItem[]
  releases: {
    latest: { html_url: string; name: string; tag_name: string } | null
    prerelease: { html_url: string; name: string; tag_name: string } | null
  }
  counts: {
    prs: number
    issues: number
    commits: number
  }
  has_data: boolean
  fetched_at?: string
}

export interface AvailableDatesResponse {
  project: string
  dates: string[]
  total: number
}

/**
 * 生成每日总结（使用长超时配置）
 */
export const generateDailySummary = async (params: GenerateSummaryParams) => {
  const response = await longTimeoutApiClient.post('/daily-summary/generate', params)
  return response.data
}

/**
 * 获取每日总结
 */
export const getDailySummary = async (project: string, date: string): Promise<DailySummary> => {
  const response = await apiClient.get(`/daily-summary/${project}/${date}`)
  return response.data
}

/**
 * 获取每日总结列表
 */
export const getDailySummaryList = async (
  project: string,
  limit: number = 30,
  offset: number = 0
) => {
  const response = await apiClient.get(`/daily-summary/${project}/list`, {
    params: { limit, offset },
  })
  return response.data
}

/**
 * 获取每日数据
 */
export const fetchDailyData = async (params: FetchDataParams) => {
  const response = await apiClient.post('/daily-summary/fetch-data', params)
  return response.data
}

/**
 * 刷新 PR 和 Issue 状态
 */
export const refreshDailyStatus = async (params: FetchDataParams) => {
  const response = await apiClient.post('/daily-summary/refresh-status', params)
  return response.data
}

/**
 * 重新生成每日总结（使用长超时配置）
 */
export const regenerateDailySummary = async (
  project: string,
  date: string,
  llm_provider?: string
) => {
  const response = await longTimeoutApiClient.post(`/daily-summary/${project}/${date}/regenerate`, null, {
    params: { llm_provider },
  })
  return response.data
}

/**
 * 获取指定日期的每日数据（PR、Issue、Commit）
 */
export const getDailyData = async (project: string, date: string): Promise<DailyDataResponse> => {
  const response = await apiClient.get(`/daily-summary/${project}/${date}/data`)
  return response.data
}

/**
 * 获取项目可用日期列表
 */
export const getAvailableDates = async (project: string, limit: number = 30): Promise<AvailableDatesResponse> => {
  const response = await apiClient.get(`/daily-summary/${project}/available-dates`, {
    params: { limit },
  })
  return response.data
}

/**
 * 获取 LLM 提供商列表
 */
export const getLLMProviders = async (): Promise<LLMProvider[]> => {
  const response = await apiClient.get('/system/config/llm-providers')
  return response.data
}

/**
 * 更新 LLM 提供商配置
 */
export const updateLLMProvider = async (provider: string, config: Partial<LLMProvider>) => {
  const response = await apiClient.put(`/system/config/llm-providers/${provider}`, config)
  return response.data
}

/**
 * 获取每日总结配置
 */
export const getDailySummaryConfig = async () => {
  const response = await apiClient.get('/system/config/daily-summary')
  return response.data
}

/**
 * 更新每日总结配置
 */
export const updateDailySummaryConfig = async (config: any) => {
  const response = await apiClient.put('/system/config/daily-summary', config)
  return response.data
}

/**
 * 获取系统提示词配置
 */
export const getSystemPromptConfig = async (scope: SystemPromptScope = 'daily_summary'): Promise<SystemPromptConfig> => {
  const response = await apiClient.get('/system/config/system-prompt', { params: { scope } })
  return response.data
}

/**
 * 更新系统提示词配置
 */
export const updateSystemPromptConfig = async (
  prompts: Record<string, string>,
  scope: SystemPromptScope = 'daily_summary'
) => {
  const response = await apiClient.put('/system/config/system-prompt', { prompts }, { params: { scope } })
  return response.data
}

export interface TrendDataItem {
  date: string
  pr_count: number
  issue_count: number
  commit_count: number
}

export interface TrendDataResponse {
  project: string
  days: number
  data: TrendDataItem[]
}

export const getTrendData = async (project: string, days: number = 7): Promise<TrendDataResponse> => {
  const response = await apiClient.get(`/daily-summary/${project}/trend`, { params: { days } })
  return response.data
}

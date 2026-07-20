import api from './api'

export interface TestHealthScore {
  overall: number
  pass_rate: number
  stability: number
  reliability: number
  timeliness: number
  coverage: number | null
  level: string
}

export interface TestCaseItem {
  id: number
  test_name: string
  test_suite: string
  module_name: string | null
  test_type: string
  category: string | null
  hardware: string | null
  card_count: number | null
  owner: string | null
  owner_email: string | null
  inference_confidence: number
  data_granularity: string
  is_flaky: boolean
  flaky_rate: number
  flaky_evidence_count: number
  pass_rate_7d: number | null
  pass_rate_30d: number | null
  avg_duration_seconds: number | null
  duration_p90_seconds: number | null
  last_pass_duration_seconds: number | null
  health_score: number | null
  health_level: string | null
  last_result: string | null
  last_run_at: string | null
  first_seen_at: string | null
  total_runs: number
  lifetime_runs: number
  lifetime_failures: number
  issues_found: number
  suspected_test_issue_count: number
  is_flaky_manual: boolean
  auto_issues_found: number
  auto_suspected_test_issue_count: number
  issues_found_override: boolean
  effective_issues_found: number
  effective_suspected_test_issue_count: number
}

export interface TestRunItem {
  id: number
  test_case_id: number
  result: string
  duration_seconds: number | null
  model_load_seconds: number | null
  test_exec_seconds: number | null
  failure_category: string | null
  failure_message: string | null
  flip_detected: boolean
  workflow_name: string | null
  job_name: string | null
  ci_job_id: number | null
  ci_run_id: number | null
  head_sha: string | null
  event: string | null
  started_at: string | null
}

export interface TestSuiteItem {
  suite_name: string
  test_type: string
  hardware: string | null
  card_count: number | null
  total_cases: number
  pass_rate: number
  health_score: number | null
  health_level: string | null
  flaky_cases: number
  avg_duration_seconds: number | null
  last_run_at: string | null
}

export interface TestOverview {
  health_score: TestHealthScore
  total_cases: number
  pass_rate_7d: number
  flaky_case_count: number
  attention_case_count: number
  avg_duration_p50: number | null
  suite_distribution: Record<string, number>
  result_distribution: Record<string, number>
  health_trend: Array<{ date: string; score: number; level: string }>
  pass_rate_trend: Array<{ date: string; rate: number }>
}

export interface FlakyCaseDetail {
  test_name: string
  test_suite: string
  module_name: string | null
  owner: string | null
  flip_rate: number
  total_runs: number
  flip_count: number
  recent_results: string[]
  suggested_action: string
}

export interface FailureBreakdown {
  product_bug: number
  test_bug: number
  infrastructure: number
  unknown: number
  total: number
  product_bug_ratio: number
  infrastructure_ratio: number
  noise_ratio: number
}

export interface OwnerMatrixItem {
  owner: string | null
  modules: string[]
  total_cases: number
  pass_rate_7d: number | null
  flaky_cases: number
  pending_failures: number
  avg_fix_hours: number | null
}

export interface ModuleHealthItem {
  module_name: string
  owner: string | null
  total_cases: number
  pass_rate_7d: number | null
  flaky_count: number
  pending_failures: number
  health_score: number | null
  health_level: string | null
}

export interface PaginatedResult<T> {
  total: number
  items: T[]
  page: number
  page_size: number
}

export const getOverview = async (days: number = 7): Promise<TestOverview> => {
  const response = await api.get<TestOverview>('/test-board/overview', { params: { days } })
  return response.data
}

export const getSuites = async (): Promise<TestSuiteItem[]> => {
  const response = await api.get<TestSuiteItem[]>('/test-board/suites')
  return response.data
}

export const getFilterOptions = async (): Promise<{
  test_types: string[]
  suites: string[]
  hardwares: string[]
}> => {
  const response = await api.get('/test-board/filter-options')
  return response.data
}

export const getCases = async (params?: {
  test_type?: string
  suite_name?: string
  module_name?: string
  hardware?: string
  result?: string
  health_level?: string
  is_flaky?: boolean
  owner?: string
  sort?: string
  order?: string
  page?: number
  per_page?: number
}): Promise<PaginatedResult<TestCaseItem>> => {
  const response = await api.get<PaginatedResult<TestCaseItem>>('/test-board/cases', { params })
  return response.data
}

export const getCaseDetail = async (caseId: number): Promise<{ case: TestCaseItem; runs: TestRunItem[] }> => {
  const response = await api.get<{ case: TestCaseItem; runs: TestRunItem[] }>(`/test-board/cases/${caseId}`)
  return response.data
}

export const getRuns = async (params?: {
  test_case_id?: number
  result?: string
  days?: number
  page?: number
  per_page?: number
}): Promise<PaginatedResult<TestRunItem>> => {
  const response = await api.get<PaginatedResult<TestRunItem>>('/test-board/runs', { params })
  return response.data
}

export const getFlakyCases = async (params?: {
  min_flip_rate?: number
  days?: number
  suite_name?: string
  module_name?: string
  sort?: string
  page?: number
  per_page?: number
}): Promise<PaginatedResult<FlakyCaseDetail>> => {
  const response = await api.get<PaginatedResult<FlakyCaseDetail>>('/test-board/flaky', { params })
  return response.data
}

export const getFailureBreakdown = async (params?: {
  days?: number
  category?: string
  suite_name?: string
}): Promise<FailureBreakdown> => {
  const response = await api.get<FailureBreakdown>('/test-board/failures', { params })
  return response.data
}

export const getDurationAnalysis = async (params?: {
  days?: number
  suite_name?: string
}): Promise<{ top_slow: Array<{ test_name: string; avg_duration: number; p90_duration: number | null }> }> => {
  const response = await api.get('/test-board/duration', { params })
  return response.data
}

export const getOwnerMatrix = async (): Promise<OwnerMatrixItem[]> => {
  const response = await api.get<OwnerMatrixItem[]>('/test-board/owners')
  return response.data
}

export const getModuleHealth = async (): Promise<ModuleHealthItem[]> => {
  const response = await api.get<ModuleHealthItem[]>('/test-board/modules')
  return response.data
}

export const getTrends = async (days: number = 30): Promise<{
  health_trend: Array<{ date: string; score: number; level: string }>
  pass_rate_trend: Array<{ date: string; rate: number }>
}> => {
  const response = await api.get('/test-board/trends', { params: { days } })
  return response.data
}

export const triggerSync = async (params: { days_back: number; force: boolean }): Promise<{ success: boolean; message: string; count: number }> => {
  const response = await api.post('/test-board/sync', params)
  return response.data
}

export const annotateFailure = async (params: { test_run_id: number; annotated_category: string; annotated_by: string }): Promise<{ success: boolean; message: string }> => {
  const response = await api.post('/test-board/annotate', params)
  return response.data
}

export interface TestCaseUpdatePayload {
  issues_found?: number
  suspected_test_issue_count?: number
  is_flaky?: boolean
  is_flaky_manual?: boolean
  owner?: string
  owner_email?: string
  use_auto_issues?: boolean
}

export const updateCase = async (caseId: number, payload: TestCaseUpdatePayload): Promise<TestCaseItem> => {
  const response = await api.patch<TestCaseItem>(`/test-board/cases/${caseId}`, payload)
  return response.data
}

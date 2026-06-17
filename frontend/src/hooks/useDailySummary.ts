/**
 * 每日总结相关的 React Hooks
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as api from '../services/dailySummary'

/**
 * 获取每日总结
 */
export const useDailySummary = (project: string, date: string) => {
  return useQuery({
    queryKey: ['daily-summary', project, date],
    queryFn: () => api.getDailySummary(project, date),
    enabled: !!project && !!date,
  })
}

/**
 * 获取每日总结列表
 */
export const useDailySummaryList = (project: string, limit: number = 30, offset: number = 0) => {
  return useQuery({
    queryKey: ['daily-summary-list', project, limit, offset],
    queryFn: () => api.getDailySummaryList(project, limit, offset),
    enabled: !!project,
  })
}

/**
 * 生成每日总结
 */
export const useGenerateDailySummary = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: api.generateDailySummary,
    onSuccess: () => {
      // 刷新相关查询
      queryClient.invalidateQueries({ queryKey: ['daily-summary'] })
      queryClient.invalidateQueries({ queryKey: ['daily-summary-list'] })
    },
    onError: (error: any) => {
      console.error('Failed to generate daily summary:', error)
    },
  })
}

/**
 * 获取每日数据
 */
export const useFetchDailyData = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: api.fetchDailyData,
    onSuccess: () => {
      // 刷新相关查询
      queryClient.invalidateQueries({ queryKey: ['daily-summary'] })
      queryClient.invalidateQueries({ queryKey: ['daily-summary-list'] })
      queryClient.invalidateQueries({ queryKey: ['daily-data'] })
      queryClient.invalidateQueries({ queryKey: ['available-dates'] })
    },
  })
}

/**
 * 刷新 PR 和 Issue 状态
 */
export const useRefreshDailyStatus = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: api.refreshDailyStatus,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['daily-data'] })
    },
  })
}

/**
 * 重新生成每日总结
 */
export const useRegenerateDailySummary = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ project, date, llm_provider }: { project: string, date: string, llm_provider?: string }) =>
      api.regenerateDailySummary(project, date, llm_provider),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['daily-summary'] })
    },
    onError: (error: any) => {
      console.error('Failed to regenerate daily summary:', error)
    },
  })
}

/**
 * 获取 LLM 提供商列表
 */
export const useLLMProviders = () => {
  return useQuery({
    queryKey: ['llm-providers'],
    queryFn: api.getLLMProviders,
  })
}

/**
 * 更新 LLM 提供商配置
 */
export const useUpdateLLMProvider = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ provider, config }: { provider: string, config: Partial<api.LLMProvider> }) =>
      api.updateLLMProvider(provider, config),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
      queryClient.invalidateQueries({ queryKey: ['daily-summary-config'] })
    },
  })
}

/**
 * 获取每日总结配置
 */
export const useDailySummaryConfig = () => {
  return useQuery({
    queryKey: ['daily-summary-config'],
    queryFn: api.getDailySummaryConfig,
  })
}

/**
 * 更新每日总结配置
 */
export const useUpdateDailySummaryConfig = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: api.updateDailySummaryConfig,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['daily-summary-config'] })
    },
  })
}

/**
 * 获取系统提示词配置
 */
export const useSystemPromptConfig = (scope: api.SystemPromptScope = 'daily_summary') => {
  return useQuery({
    queryKey: ['system-prompt-config', scope],
    queryFn: () => api.getSystemPromptConfig(scope),
  })
}

/**
 * 更新系统提示词配置
 */
export const useUpdateSystemPromptConfig = () => {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      prompts,
      scope = 'daily_summary',
    }: {
      prompts: Record<string, string>
      scope?: api.SystemPromptScope
    }) => api.updateSystemPromptConfig(prompts, scope),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['system-prompt-config', variables.scope || 'daily_summary'] })
    },
  })
}

/**
 * 获取指定日期的每日数据（PR、Issue、Commit）
 */
export const useDailyData = (project: string, date: string) => {
  return useQuery({
    queryKey: ['daily-data', project, date],
    queryFn: () => api.getDailyData(project, date),
    enabled: !!project && !!date,
  })
}

/**
 * 获取项目可用日期列表
 */
export const useAvailableDates = (project: string, limit: number = 30) => {
  return useQuery({
    queryKey: ['available-dates', project],
    queryFn: () => api.getAvailableDates(project, limit),
    enabled: !!project,
  })
}

export const useTrendData = (project: string, days: number = 7) => {
  return useQuery({
    queryKey: ['trend-data', project, days],
    queryFn: () => api.getTrendData(project, days),
    enabled: !!project,
  })
}

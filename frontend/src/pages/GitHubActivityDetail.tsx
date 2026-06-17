import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Card, Table, Space, Tag, Typography, Button, Tooltip, Alert, message, Tabs, Empty, DatePicker, Modal, Segmented } from 'antd'
import {
  GithubOutlined,
  PullRequestOutlined,
  IssuesCloseOutlined,
  CheckCircleOutlined,
  ArrowLeftOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  CalendarOutlined,
  SyncOutlined,
  ExclamationCircleOutlined,
  LineChartOutlined,
} from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCurrentUser } from '../hooks/useCurrentUser'
import { useDailyData, useFetchDailyData, useRefreshDailyStatus, useLLMProviders, useTrendData } from '../hooks/useDailySummary'
import { useCommitAnalysisBatch } from '../hooks/useCommitAnalysis'
import { AISummaryTab } from '../components/AISummaryTab'
import { DailyDataItem, DailyCommitItem, GitHubActor } from '../services/dailySummary'
import { ANALYSIS_STATUSES, CHANGE_TYPES, CommitAnalysisBatchItem, CommitAnalysisStatus, CommitChangeType } from '../services/commitAnalysis'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend, ResponsiveContainer } from 'recharts'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import relativeTime from 'dayjs/plugin/relativeTime'
import '../components/GitHubActivityPanel.css'

dayjs.locale('zh-cn')
dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.extend(relativeTime)

// 使用北京时间
const BEIJING_TIMEZONE = 'Asia/Shanghai'

const { Text, Title } = Typography

const getActorName = (actor: GitHubActor) => {
  if (!actor) return '-'
  return typeof actor === 'string' ? actor : actor.login
}

const getStatusColor = (status: CommitAnalysisStatus) => {
  if (status === '已闭环') return 'green'
  if (status === '已分析') return 'blue'
  return 'orange'
}

const getChangeTypeColor = (type: CommitChangeType) => {
  const colors: Record<CommitChangeType, string> = {
    Feature: 'green',
    Bugfix: 'red',
    Refactor: 'purple',
    Common: 'default',
    Test: 'cyan',
    CI: 'gold',
    Other: 'default',
  }
  return colors[type]
}

type CommitTableItem = DailyCommitItem & {
  analysis?: CommitAnalysisBatchItem
}

function GitHubActivityDetail() {
  const { project } = useParams<{ project: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data: currentUser } = useCurrentUser()

  // 判断是否是超级管理员
  const isSuperAdmin = currentUser?.role === 'super_admin'

  // 默认显示前一天
  const [selectedDate, setSelectedDate] = useState<string>(dayjs().subtract(1, 'day').format('YYYY-MM-DD'))
  const [isFetching, setIsFetching] = useState(false)
  const [isConfirmVisible, setIsConfirmVisible] = useState(false)
  const [trendDays, setTrendDays] = useState<number>(7)

  // 获取每日数据
  const { data, isLoading, refetch } = useDailyData(project || '', selectedDate)

  const { data: llmProviders } = useLLMProviders()

  const { data: trendData, isLoading: isTrendLoading } = useTrendData(project || '', trendDays)

  // 判断当天是否已有数据
  const hasData = data?.has_data || (data?.counts && (data.counts.prs > 0 || data.counts.issues > 0 || data.counts.commits > 0))
  const isRefresh = hasData

  // 手动触发 mutation
  const fetchDailyDataMutation = useFetchDailyData()
  const refreshStatusMutation = useRefreshDailyStatus()

  // 处理日期变更
  const handleDateChange = (date: dayjs.Dayjs | null) => {
    if (date) {
      setSelectedDate(date.format('YYYY-MM-DD'))
    }
  }

  // 显示确认对话框
  const showConfirm = () => {
    setIsConfirmVisible(true)
  }

  // 处理手动采集/重新采集数据
  const handleFetchData = async () => {
    setIsFetching(true)
    try {
      await fetchDailyDataMutation.mutateAsync({
        project: project as string,
        date: selectedDate,
        force_refresh: isRefresh, // 已有数据时使用 force_refresh
      })
      message.success(isRefresh ? `数据已重新采集：${selectedDate}` : `数据已采集：${selectedDate}`)
      // 刷新数据
      refetch()
      queryClient.invalidateQueries({ queryKey: ['daily-data', project, selectedDate] })
      queryClient.invalidateQueries({ queryKey: ['available-dates', project] })
    } catch (error: any) {
      message.error(error.response?.data?.detail || '采集失败')
    } finally {
      setIsFetching(false)
      setIsConfirmVisible(false)
    }
  }

  // 处理刷新数据（仅更新 PR 和 Issue 状态）
  const handleRefresh = async () => {
    setIsFetching(true)
    try {
      await refreshStatusMutation.mutateAsync({
        project: project as string,
        date: selectedDate,
      })
      message.success(`状态已刷新：${selectedDate}`)
      // 刷新数据
      refetch()
      queryClient.invalidateQueries({ queryKey: ['daily-data', project, selectedDate] })
    } catch (error: any) {
      message.error(error.response?.data?.detail || '刷新失败')
    } finally {
      setIsFetching(false)
    }
  }

  const projectTitle = project === 'vllm' ? 'vLLM' : 'vLLM Ascend'
  const projectColor = project === 'vllm' ? '#722ed1' : '#1890ff'

  // PR 表格列
  const prColumns = [
    {
      title: 'PR',
      dataIndex: 'number',
      key: 'number',
      width: 100,
      sorter: (a: DailyDataItem, b: DailyDataItem) => a.number - b.number,
      render: (num: number, record: DailyDataItem) => (
        <a
          href={record.html_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: '#1890ff', whiteSpace: 'nowrap' }}
        >
          #{num}
        </a>
      ),
    },
    {
      title: '标题',
      dataIndex: 'title',
      key: 'title',
      ellipsis: true,
      sorter: (a: DailyDataItem, b: DailyDataItem) => a.title.localeCompare(b.title),
      render: (title: string) => <Text>{title}</Text>,
    },
    {
      title: '作者',
      dataIndex: 'user',
      key: 'user',
      width: 120,
      sorter: (a: DailyDataItem, b: DailyDataItem) => getActorName(a.user).localeCompare(getActorName(b.user)),
      render: (user: GitHubActor) => (
        <Tag color="blue">{getActorName(user)}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      width: 100,
      sorter: (a: DailyDataItem, b: DailyDataItem) => a.state.localeCompare(b.state),
      render: (state: string, record: DailyDataItem) => {
        // GitHub PR state 可能是 'open' 或 'closed'
        // 需要检查 merged_at 或 merged 字段来判断是已合入还是已关闭
        const isMerged = record.merged_at !== null && record.merged_at !== undefined;
        
        if (state === 'open') {
          return <Tag color="green">打开</Tag>;
        } else if (isMerged) {
          return <Tag color="purple">已合入</Tag>;
        } else {
          return <Tag color="default">已关闭</Tag>;
        }
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      sorter: (a: DailyDataItem, b: DailyDataItem) =>
        new Date(a.created_at || '').getTime() - new Date(b.created_at || '').getTime(),
      render: (date: string | null) => date ? (
        <Space direction="vertical" size={0}>
          <Text>{dayjs(date).tz(BEIJING_TIMEZONE).format('YYYY-MM-DD HH:mm')}</Text>
        </Space>
      ) : '-',
    },
  ]

  // Issue 表格列
  const issueColumns = [
    {
      title: 'Issue',
      dataIndex: 'number',
      key: 'number',
      width: 100,
      render: (num: number, record: DailyDataItem) => (
        <a
          href={record.html_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: '#fa8c16', whiteSpace: 'nowrap' }}
        >
          #{num}
        </a>
      ),
    },
    {
      title: '标题',
      dataIndex: 'title',
      key: 'title',
      ellipsis: true,
      render: (title: string, record: DailyDataItem) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text>{title}</Text>
          {record.labels && record.labels.length > 0 && (
            <Space size={4} wrap>
              {record.labels.map((label) => (
                <Tag
                  key={label.name}
                  color={`#${label.color}`}
                  title={label.description}
                  style={{ fontSize: 11, padding: '0 8px' }}
                >
                  {label.name}
                </Tag>
              ))}
            </Space>
          )}
        </Space>
      ),
    },
    {
      title: '作者',
      dataIndex: 'user',
      key: 'user',
      width: 120,
      render: (user: GitHubActor) => (
        <Tag color="blue">{getActorName(user)}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'state',
      key: 'state',
      width: 100,
      render: (state: string) => (
        <Tag color={state === 'open' ? 'green' : 'default'}>
          {state === 'open' ? '打开' : '关闭'}
        </Tag>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (date: string | null) => date ? (
        <Space direction="vertical" size={0}>
          <Text>{dayjs(date).tz(BEIJING_TIMEZONE).format('YYYY-MM-DD HH:mm')}</Text>
        </Space>
      ) : '-',
    },
  ]

  const commitShas = (data?.commits || []).map((commit) => commit.sha)
  const { data: commitAnalysisData, isLoading: isCommitAnalysisLoading } = useCommitAnalysisBatch(project || '', commitShas)

  // Commit 表格列
  const commitColumns = [
    {
      title: 'SHA',
      dataIndex: 'sha',
      key: 'sha',
      width: 100,
      render: (sha: string, record: CommitTableItem) => (
        <a
          href={record.html_url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => event.stopPropagation()}
          style={{ fontFamily: 'monospace', color: '#52c41a' }}
        >
          {sha.slice(0, 7)}
        </a>
      ),
    },
    {
      title: '提交信息',
      dataIndex: 'message',
      key: 'message',
      ellipsis: true,
      render: (message: string, record: CommitTableItem) => (
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          <Text ellipsis>{message}</Text>
          {record.pr_number && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              来自 PR #{record.pr_number}: {record.pr_title}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: '作者',
      dataIndex: 'author',
      key: 'author',
      width: 120,
      render: (author: GitHubActor) => <Tag color="geekblue">{getActorName(author)}</Tag>,
    },
    {
      title: '责任人',
      key: 'assignee',
      width: 120,
      filters: (commitAnalysisData?.filters.assignees || []).map((assignee) => ({ text: assignee, value: assignee })),
      onFilter: (value: any, record: CommitTableItem) => (record.analysis?.assignee || '') === value,
      render: (_: unknown, record: CommitTableItem) => record.analysis?.assignee ? <Tag>{record.analysis.assignee}</Tag> : '-',
    },
    {
      title: '类型',
      key: 'change_type',
      width: 120,
      filters: CHANGE_TYPES.map((type) => ({ text: type, value: type })),
      onFilter: (value: any, record: CommitTableItem) => record.analysis?.change_type === value,
      render: (_: unknown, record: CommitTableItem) => record.analysis?.change_type ? (
        <Tag color={getChangeTypeColor(record.analysis.change_type)}>{record.analysis.change_type}</Tag>
      ) : '-',
    },
    {
      title: '状态',
      key: 'analysis_status',
      width: 110,
      filters: ANALYSIS_STATUSES.map((status) => ({ text: status, value: status })),
      onFilter: (value: any, record: CommitTableItem) => (record.analysis?.status || '未分析') === value,
      render: (_: unknown, record: CommitTableItem) => {
        const status = record.analysis?.status || '未分析'
        return <Tag color={getStatusColor(status)}>{status}</Tag>
      },
    },
    {
      title: '提交时间',
      dataIndex: 'committed_at',
      key: 'committed_at',
      width: 180,
      render: (date: string | null) => date ? (
        <Text>{dayjs(date).tz(BEIJING_TIMEZONE).format('YYYY-MM-DD HH:mm')}</Text>
      ) : '-',
    },
  ]

  // 数据安全访问
  const pullRequests = data?.pull_requests || []
  const issues = data?.issues || []
  const commits: CommitTableItem[] = (data?.commits || []).map((commit) => ({
    ...commit,
    analysis: commitAnalysisData?.analyses[commit.sha],
  }))
  const counts = data?.counts || { prs: 0, issues: 0, commits: 0 }
  const fetchedAt = data?.fetched_at

  return (
    <div className="stripe-page-container">
      {/* 页面标题 */}
      <div className="stripe-page-header">
        <Button
          type="default"
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/')}
          className="stripe-btn-ghost stripe-btn-sm"
        >
          返回
        </Button>
        <Title level={3} className="stripe-page-title" style={{ margin: 0 }}>
          <GithubOutlined style={{ color: projectColor, marginRight: 8 }} />
          {projectTitle} - 项目动态
        </Title>
        <Space style={{ marginLeft: 'auto' }}>
          {/* 日期选择器 */}
          <Space>
            <CalendarOutlined style={{ color: '#8c8c8c' }} />
            <DatePicker
              value={dayjs(selectedDate)}
              onChange={handleDateChange}
              format="YYYY-MM-DD"
              placeholder="选择日期"
              style={{ width: 150 }}
              disabledDate={(current) => current && current > dayjs().endOf('day')}
            />
          </Space>
          {/* 手动采集按钮：仅超级管理员可见 */}
          {isSuperAdmin && (
            <Tooltip title={isRefresh ? '重新采集当前日期的 GitHub 数据（覆盖已有数据）' : '采集当前日期的 GitHub 数据'}>
              <Button
                icon={<ThunderboltOutlined />}
                onClick={showConfirm}
                loading={isFetching}
              >
                {isRefresh ? '重新采集' : '采集数据'}
              </Button>
            </Tooltip>
          )}
          <Tooltip title="刷新PR/ISSUE状态">
            <Button
              icon={<SyncOutlined spin={isFetching} />}
              onClick={handleRefresh}
              loading={isFetching}
            >
              刷新
            </Button>
          </Tooltip>
        </Space>
      </div>

      {/* 说明提示 */}
      <Alert
        message={`显示 ${selectedDate} 的项目动态数据${fetchedAt ? `（采集时间：${dayjs(fetchedAt).tz(BEIJING_TIMEZONE).format('YYYY-MM-DD HH:mm')}）` : ''}。`}
        type="info"
        showIcon
        style={{ marginBottom: 24 }}
        closable
      />

      {/* 数据列表 Tab */}
      <Card className="stripe-card">
        <Tabs
          defaultActiveKey="pr"
          className="stripe-page-tabs"
          items={[
            {
              key: 'pr',
              label: (
                <Space>
                  <PullRequestOutlined style={{ color: '#1890ff' }} />
                  <span>新增 PR</span>
                  <Tag color="blue">{counts.prs}</Tag>
                </Space>
              ),
              children: pullRequests.length > 0 ? (
                <Table
                  columns={prColumns}
                  dataSource={pullRequests}
                  loading={isLoading}
                  rowKey="number"
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 800 }}
                />
              ) : (
                <Empty description="当日无新增 PR" />
              ),
            },
            {
              key: 'issue',
              label: (
                <Space>
                  <IssuesCloseOutlined style={{ color: '#fa8c16' }} />
                  <span>新增 Issue</span>
                  <Tag color="orange">{counts.issues}</Tag>
                </Space>
              ),
              children: issues.length > 0 ? (
                <Table
                  columns={issueColumns}
                  dataSource={issues}
                  loading={isLoading}
                  rowKey="number"
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 800 }}
                />
              ) : (
                <Empty description="当日无新增 Issue" />
              ),
            },
            {
              key: 'commit',
              label: (
                <Space>
                  <CheckCircleOutlined style={{ color: '#52c41a' }} />
                  <span>Commit</span>
                  <Tag color="green">{counts.commits}</Tag>
                </Space>
              ),
              children: commits.length > 0 ? (
                <Table
                  columns={commitColumns}
                  dataSource={commits}
                  loading={isLoading || isCommitAnalysisLoading}
                  rowKey="sha"
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 1100 }}
                  onRow={(record) => ({
                    onClick: () => navigate(`/github-activity/${project}/commits/${record.sha}?date=${selectedDate}`),
                    style: { cursor: 'pointer' },
                  })}
                />
              ) : (
                <Empty description="当日无 Commit" />
              ),
            },
            {
              key: 'trend',
              label: (
                <Space>
                  <LineChartOutlined style={{ color: '#13c2c2' }} />
                  <span>趋势图</span>
                </Space>
              ),
              children: (
                <div style={{ padding: '16px 0' }}>
                  <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Text type="secondary">近 {trendDays} 天 PR/Issue/Commit 数量趋势（点击下方标签可切换线条显示/隐藏）</Text>
                    <Segmented
                      options={[
                        { label: '7天', value: 7 },
                        { label: '30天', value: 30 },
                        { label: '90天', value: 90 },
                      ]}
                      value={trendDays}
                      onChange={(val) => setTrendDays(val as number)}
                    />
                  </div>
                  {isTrendLoading ? (
                    <div style={{ textAlign: 'center', padding: 40 }}>
                      <SyncOutlined spin style={{ fontSize: 24, color: '#1890ff' }} />
                      <Text style={{ marginLeft: 8 }}>加载趋势数据...</Text>
                    </div>
                  ) : trendData?.data && trendData.data.length > 0 ? (
                    <ResponsiveContainer width="100%" height={400}>
                      <LineChart data={trendData.data} margin={{ top: 20, right: 30, left: 20, bottom: 5 }}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis
                          dataKey="date"
                          tickFormatter={(v: string) => dayjs(v).format('MM-DD')}
                        />
                        <YAxis allowDecimals={false} />
                        <RechartsTooltip
                          labelFormatter={(label: string) => dayjs(label).format('YYYY-MM-DD')}
                          formatter={(value: number, name: string) => {
                            const labels: Record<string, string> = { pr_count: 'PR', issue_count: 'Issue', commit_count: 'Commit' }
                            return [value, labels[name] || name]
                          }}
                        />
                        <Legend
                          formatter={(value: string) => {
                            const labels: Record<string, string> = { pr_count: 'PR', issue_count: 'Issue', commit_count: 'Commit' }
                            return labels[value] || value
                          }}
                        />
                        <Line type="monotone" dataKey="pr_count" stroke="#1890ff" strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }}
                          label={({ x, y, value, index }: any) => value > 0 ? <text key={`pr-${index}`} x={x} y={y - 12} fill="#1890ff" fontSize={11} textAnchor="middle" fontWeight={500}>{value}</text> : <g key={`pr-empty-${index}`} />}
                        />
                        <Line type="monotone" dataKey="issue_count" stroke="#fa8c16" strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }}
                          label={({ x, y, value, index }: any) => value > 0 ? <text key={`issue-${index}`} x={x} y={y - 12} fill="#fa8c16" fontSize={11} textAnchor="middle" fontWeight={500}>{value}</text> : <g key={`issue-empty-${index}`} />}
                        />
                        <Line type="monotone" dataKey="commit_count" stroke="#52c41a" strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }}
                          label={({ x, y, value, index }: any) => value > 0 ? <text key={`commit-${index}`} x={x} y={y - 12} fill="#52c41a" fontSize={11} textAnchor="middle" fontWeight={500}>{value}</text> : <g key={`commit-empty-${index}`} />}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  ) : (
                    <Empty description="暂无趋势数据" />
                  )}
                </div>
              ),
            },
            {
              key: 'summary',
              label: (
                <Space>
                  <RobotOutlined style={{ color: '#722ed1' }} />
                  <span>AI 总结</span>
                </Space>
              ),
              children: (
                <AISummaryTab project={project} date={selectedDate} isSuperAdmin={isSuperAdmin} />
              ),
            },
          ]}
        />
      </Card>

      {/* 采集数据确认对话框 */}
      <Modal
        title={
          <Space>
            <ExclamationCircleOutlined style={{ color: '#faad14' }} />
            <span>确认采集数据</span>
          </Space>
        }
        open={isConfirmVisible}
        onCancel={() => setIsConfirmVisible(false)}
        footer={[
          <Button key="cancel" onClick={() => setIsConfirmVisible(false)}>
            取消
          </Button>,
          <Button
            key="confirm"
            type="primary"
            onClick={handleFetchData}
            loading={isFetching}
          >
            {isFetching ? '采集中...' : '确认'}
          </Button>,
        ]}
      >
        <div style={{ padding: '16px 0' }}>
          <p>
            {isRefresh ? (
              <>
                确定要重新采集 <strong>{selectedDate}</strong> 的 GitHub 数据吗？
                <br />
                <span style={{ color: '#faad14' }}>
                  这将删除并重新采集当天的所有 PR、Issue 和 Commit 数据。
                </span>
              </>
            ) : (
              <>
                确定要采集 <strong>{selectedDate}</strong> 的 GitHub 数据吗？
                <br />
                <span style={{ color: '#8c8c8c' }}>
                  将采集当天的所有 PR、Issue 和 Commit 数据。
                </span>
              </>
            )}
          </p>
        </div>
      </Modal>
    </div>
  )
}

export default GitHubActivityDetail
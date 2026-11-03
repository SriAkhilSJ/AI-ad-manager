/**
 * Typed API client.
 *
 * - All requests are relative (`/api/v1/...`) and share the Vite proxy, so the
 *   browser never needs to know the backend host.
 * - Errors are normalised into `ApiError` carrying the backend's structured
 *   envelope (`code`, `message`, `details`, `request_id`) so the UI can render a
 *   precise reason instead of "something went wrong".
 * - Sensitive mutations accept an `Idempotency-Key`.
 */

import type {
  AgentRunSummary,
  AgentSpec,
  AnalyticsOverview,
  ApiErrorBody,
  ApprovalRecord,
  BrandProfile,
  Campaign,
  CampaignDetail,
  Competitor,
  ContentItem,
  ContentItemDetail,
  ContentPillar,
  ContentVersion,
  CreativeAsset,
  GraphTopology,
  MarketingGoal,
  PlatformStatus,
  PlatformSpec,
  StudioCapabilities,
  StudioOverview,
  GenerateRequest,
  GeneratedItem,
  ContentPlan,
  ContentPlanDetail,
  CreativeAssetDetail,
  CreativeAssetStudio,
  CreativeBrief,
  CreativeCapabilities,
  CreativeFlowStatus,
  CreativeJob,
  CreativeOverview,
  CreativeQaReport,
  PlanRequest,
  VariantSet,
  ValidationResult,
  CalendarView,
  CalendarEntry,
  PublishedPost,
  ResearchCapabilities,
  ResearchFinding,
  ResearchFindingPage,
  ResearchProject,
  ResearchProjectDetail,
  ResearchRunResult,
  CompetitorSnapshot,
  Role,
  ScheduledPost,
  SocialAccount,
  StrategyCapabilities,
  StrategyDetail,
  StrategySummary,
  StrategyVersion,
  StrategyWorkflowStartResponse,
  StrategyWorkflowState,
  SystemStatus,
  TokenResponse,
  UsageSummary,
  User,
  VersionDiff,
  WorkflowStatus,
  WorkflowSummary,
} from './types'

const API_BASE = '/api/v1'
const TOKEN_KEY = 'crusor.hq.token'
const USER_KEY = 'crusor.hq.user'

export class ApiError extends Error {
  code: string
  details: Record<string, unknown>
  requestId: string
  status: number

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId = '',
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }

  /** True when the action is blocked by policy rather than by a transient fault. */
  get isPolicyBlock(): boolean {
    return [
      'approval_required',
      'publishing_disabled',
      'invalid_state_transition',
      'forbidden',
      'integration_unavailable',
      'idempotency_key_reuse',
    ].includes(this.code)
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function getStoredUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

export function setStoredUser(user: User | null) {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
  else localStorage.removeItem(USER_KEY)
}

type QueryValue = string | number | boolean | null | undefined

function buildQuery(query?: Record<string, QueryValue>): string {
  if (!query) return ''
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value))
    }
  })
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, QueryValue>
  idempotencyKey?: string
  formData?: FormData
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, idempotencyKey, formData, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  if (body !== undefined && !formData) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(query)}`, {
      method,
      headers,
      body: formData ?? (body !== undefined ? JSON.stringify(body) : undefined),
      signal,
    })
  } catch (cause) {
    if ((cause as Error)?.name === 'AbortError') throw cause
    throw new ApiError(
      0,
      'network_error',
      'Cannot reach the Marketing HQ API. Check that the backend is running.',
    )
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = undefined
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = undefined
    }
  }

  if (!response.ok) {
    const envelope = payload as ApiErrorBody | undefined
    const err = envelope?.error
    if (response.status === 401) {
      setToken(null)
      setStoredUser(null)
    }
    throw new ApiError(
      response.status,
      err?.code ?? `http_${response.status}`,
      err?.message ?? `Request failed (${response.status}).`,
      err?.details ?? {},
      err?.request_id ?? response.headers.get('X-Request-Id') ?? '',
    )
  }

  return payload as T
}

export function newIdempotencyKey(prefix = 'hq'): string {
  const rand = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`
  return `${prefix}-${rand}`
}

// ------------------------------------------------------------------ auth
export const authApi = {
  login: (email: string, password: string) =>
    request<TokenResponse>('/auth/token', { method: 'POST', body: { email, password } }),
  me: () => request<User>('/auth/me'),
  createUser: (payload: { email: string; password: string; full_name?: string; role: Role }) =>
    request<User>('/auth/users', { method: 'POST', body: payload }),
}

// ----------------------------------------------------------------- system
export const systemApi = {
  status: () => request<SystemStatus>('/system/status'),
  health: () => request<{ status: string; uptime_seconds: number }>('/system/health'),
}

// ------------------------------------------------------------------ brand
export const brandApi = {
  list: () => request<BrandProfile[]>('/brand'),
  active: () => request<BrandProfile>('/brand/active'),
  get: (id: string) => request<BrandProfile>(`/brand/${id}`),
  create: (payload: Partial<BrandProfile>) =>
    request<BrandProfile>('/brand', { method: 'POST', body: payload }),
  update: (id: string, payload: Partial<BrandProfile>) =>
    request<BrandProfile>(`/brand/${id}`, { method: 'PATCH', body: payload }),
  goals: (id: string) => request<MarketingGoal[]>(`/brand/${id}/goals`),
  createGoal: (id: string, payload: Partial<MarketingGoal>) =>
    request<MarketingGoal>(`/brand/${id}/goals`, { method: 'POST', body: payload }),
  pillars: (id: string) => request<ContentPillar[]>(`/brand/${id}/pillars`),
  createPillar: (id: string, payload: Partial<ContentPillar>) =>
    request<ContentPillar>(`/brand/${id}/pillars`, { method: 'POST', body: payload }),
  competitors: (id: string) => request<Competitor[]>(`/brand/${id}/competitors`),
  createCompetitor: (id: string, payload: Partial<Competitor>) =>
    request<Competitor>(`/brand/${id}/competitors`, { method: 'POST', body: payload }),
}

// -------------------------------------------------------------- campaigns
export const campaignApi = {
  list: (query?: { brand_profile_id?: string; status_filter?: string }) =>
    request<Campaign[]>('/campaigns', { query }),
  get: (id: string) => request<CampaignDetail>(`/campaigns/${id}`),
  create: (payload: {
    brand_profile_id: string
    name: string
    objective?: string
    brief?: string
    platforms?: string[]
  }) => request<Campaign>('/campaigns', { method: 'POST', body: payload }),
  update: (id: string, payload: Partial<Campaign>) =>
    request<Campaign>(`/campaigns/${id}`, { method: 'PATCH', body: payload }),
}

// ---------------------------------------------------------------- content
export const contentApi = {
  list: (query?: {
    brand_profile_id?: string
    campaign_id?: string
    status_filter?: string
    platform?: string
    limit?: number
    offset?: number
  }) => request<ContentItem[]>('/content', { query }),
  get: (id: string) => request<ContentItemDetail>(`/content/${id}`),
  create: (payload: {
    brand_profile_id: string
    title: string
    platform: string
    campaign_id?: string | null
    body?: string
    headline?: string
    hashtags?: string[]
    call_to_action?: string
  }) => request<ContentItemDetail>('/content', { method: 'POST', body: payload }),
  versions: (id: string) => request<ContentVersion[]>(`/content/${id}/versions`),
  createVersion: (
    id: string,
    payload: {
      body?: string
      headline?: string
      hashtags?: string[]
      call_to_action?: string
      change_note?: string
    },
  ) => request<ContentItemDetail>(`/content/${id}/versions`, { method: 'POST', body: payload }),
  approvals: (id: string) => request<ApprovalRecord[]>(`/content/${id}/approvals`),
  transition: (
    id: string,
    payload: { action: string; reason?: string; content_version_id?: string },
    idempotencyKey?: string,
  ) =>
    request<ContentItemDetail>(`/content/${id}/transition`, {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  schedule: (
    id: string,
    payload: { social_account_id: string; scheduled_for: string },
    idempotencyKey?: string,
  ) =>
    request<ScheduledPost>(`/content/${id}/schedule`, {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  publish: (
    id: string,
    payload: { social_account_id?: string } = {},
    idempotencyKey?: string,
  ) =>
    request<{
      content_item_id: string
      status: string
      published_post_id: string | null
      external_post_id: string | null
      approval_record_id: string | null
      message: string
    }>(`/content/${id}/publish`, { method: 'POST', body: payload, idempotencyKey }),
  publications: (id: string) => request<PublishedPost[]>(`/content/${id}/publications`),
  cancel: (id: string) => request<void>(`/content/${id}`, { method: 'DELETE' }),
}


// ---------------------------------------------------------- content studio
export const contentStudioApi = {
  platforms: () => request<PlatformSpec[]>('/content-studio/platforms'),
  capabilities: () => request<StudioCapabilities>('/content-studio/capabilities'),
  overview: () => request<StudioOverview>('/content-studio/overview'),
  generate: (payload: GenerateRequest, idempotencyKey?: string) =>
    request<GeneratedItem>('/content-studio/generate', {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  plans: (query?: { campaign_id?: string; limit?: number }) =>
    request<ContentPlan[]>('/content-studio/plans', { query }),
  plan: (id: string) => request<ContentPlanDetail>(`/content-studio/plans/${id}`),
  createPlan: (payload: PlanRequest, idempotencyKey?: string) =>
    request<ContentPlanDetail>('/content-studio/plans', {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  generatePlan: (id: string, force = false, idempotencyKey?: string) =>
    request<ContentPlanDetail>(`/content-studio/plans/${id}/generate`, {
      method: 'POST',
      query: { force },
      idempotencyKey,
    }),
  variants: (itemId: string) => request<VariantSet>(`/content-studio/items/${itemId}/variants`),
  regenerateVariants: (itemId: string, variants = 2, changeNote?: string) =>
    request<VariantSet & { approvals_invalidated: number; note: string }>(
      `/content-studio/items/${itemId}/variants/regenerate`,
      { method: 'POST', body: { variants, change_note: changeNote ?? '' } },
    ),
  selectVariant: (itemId: string, variantIndex: number, changeNote?: string) =>
    request<{
      content_item_id: string
      version_id: string
      version_number: number
      selected_variant: number
      status: string
      approval_invalidated: boolean
    }>(`/content-studio/items/${itemId}/variants/select`, {
      method: 'POST',
      body: { variant_index: variantIndex, change_note: changeNote ?? '' },
    }),
  validate: (itemId: string) =>
    request<ValidationResult>(`/content-studio/items/${itemId}/validate`, { method: 'POST' }),
  validations: (itemId: string) =>
    request<ValidationResult[]>(`/content-studio/items/${itemId}/validations`),
  calendar: (query?: {
    view?: 'month' | 'week' | 'list'
    anchor?: string
    platform?: string
    campaign_id?: string
  }) => request<CalendarView>('/content-studio/calendar', { query }),
  moveCalendar: (
    itemId: string,
    payload: { planned_date?: string; planned_time?: string; note?: string },
  ) =>
    request<CalendarEntry>(`/content-studio/calendar/${itemId}`, {
      method: 'PATCH',
      body: payload,
    }),
}

// --------------------------------------------------------------- creative
export const creativeApi = {
  list: (query?: { content_item_id?: string }) =>
    request<CreativeAsset[]>('/creative', { query }),
  upload: (params: {
    file: File
    contentItemId?: string
    kind?: string
    prompt?: string
  }): Promise<CreativeAsset> => {
    const form = new FormData()
    form.append('file', params.file)
    if (params.contentItemId) form.append('content_item_id', params.contentItemId)
    if (params.kind) form.append('kind', params.kind)
    if (params.prompt) form.append('prompt', params.prompt)
    return request<CreativeAsset>('/creative/upload', { method: 'POST', formData: form })
  },
  replace: (assetId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{
      asset: CreativeAsset
      invalidated_approvals: number
      message: string
    }>(`/creative/${assetId}/replace`, { method: 'POST', formData: form })
  },
}

// ----------------------------------------------------------------- social
export const socialApi = {
  platforms: () => request<PlatformStatus[]>('/social/platforms'),
  accounts: (query?: { brand_profile_id?: string }) =>
    request<SocialAccount[]>('/social/accounts', { query }),
  createAccount: (payload: {
    brand_profile_id: string
    platform: string
    handle: string
    display_name?: string
  }) => request<SocialAccount>('/social/accounts', { method: 'POST', body: payload }),
  revoke: (id: string) =>
    request<SocialAccount>(`/social/accounts/${id}/revoke`, { method: 'POST' }),
  scheduled: () => request<ScheduledPost[]>('/social/scheduled'),
  published: () => request<PublishedPost[]>('/social/published'),
}

// --------------------------------------------------------------- director
export const directorApi = {
  topology: () => request<GraphTopology>('/director/topology'),
  workflows: () => request<WorkflowSummary[]>('/director/workflows'),
  status: (id: string) => request<WorkflowStatus>(`/director/workflows/${id}`),
  start: (
    payload: { objective: string; platforms: string[]; brand_profile_id?: string },
    idempotencyKey?: string,
  ) =>
    request<{ workflow_id: string; state: Record<string, unknown> }>('/director/workflows', {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  decide: (id: string, payload: { action: string; reason?: string }) =>
    request<WorkflowStatus>(`/director/workflows/${id}/decision`, {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('decision'),
    }),
  remove: (id: string) => request<void>(`/director/workflows/${id}`, { method: 'DELETE' }),
}

// ----------------------------------------------------------------- agents
export const agentsApi = {
  list: () => request<AgentSpec[]>('/agents'),
  runs: (limit = 20) => request<AgentRunSummary[]>('/agents/runs', { query: { limit } }),
  run: (name: string, payload: Record<string, unknown>) =>
    request<{
      agent: string
      status: string
      output: Record<string, unknown> | null
      error: string | null
      run_id: string | null
      trace: Array<Record<string, unknown>>
    }>(`/agents/${name}/run`, { method: 'POST', body: { payload } }),
}

// -------------------------------------------------------------- analytics
export const analyticsApi = {
  overview: (query?: { days?: number; platform?: string }) =>
    request<AnalyticsOverview>('/analytics/overview', { query }),
  usage: (days = 30) => request<UsageSummary>('/usage/summary', { query: { days } }),
  agentRuns: (limit = 20) =>
    request<AgentRunSummary[]>('/usage/agent-runs', { query: { limit } }),
}

// ===================================================================== research
export const researchApi = {
  capabilities: () => request<ResearchCapabilities>('/research/capabilities'),

  projects: (query?: { brand_profile_id?: string; status?: string; limit?: number }) =>
    request<ResearchProject[]>('/research/projects', { query }),

  project: (id: string) => request<ResearchProjectDetail>(`/research/projects/${id}`),

  create: (payload: {
    brand_profile_id: string
    objective: string
    categories?: string[]
    max_cost_usd?: number | null
    run_now?: boolean
  }) =>
    request<ResearchRunResult>('/research/projects', {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('research'),
    }),

  run: (id: string) =>
    request<ResearchRunResult>(`/research/projects/${id}/run`, {
      method: 'POST',
      idempotencyKey: newIdempotencyKey('research-run'),
    }),

  findings: (query: {
    brand_profile_id?: string
    project_id?: string
    category?: string
    verification_status?: string
    min_confidence?: number
    competitor?: string
    limit?: number
    offset?: number
  }) => request<ResearchFindingPage>('/research/findings', { query }),

  verify: (id: string, sourceUrl: string) =>
    request<ResearchFinding>(`/research/findings/${id}/verify`, {
      method: 'PATCH',
      query: { source_url: sourceUrl },
    }),

  competitors: (brandProfileId: string) =>
    request<CompetitorSnapshot[]>('/research/competitors', {
      query: { brand_profile_id: brandProfileId },
    }),
}

// ===================================================================== strategy
export const strategyApi = {
  capabilities: () => request<StrategyCapabilities>('/strategy/capabilities'),

  list: (query?: { brand_profile_id?: string; status?: string; limit?: number }) =>
    request<StrategySummary[]>('/strategy/', { query }),

  get: (id: string) => request<StrategyDetail>(`/strategy/${id}`),

  generate: (payload: {
    brand_profile_id: string
    objective: string
    business_objectives?: string[]
    primary_objective_type?: string
    research_project_id?: string | null
    platforms?: string[]
    name?: string
    max_cost_usd?: number | null
  }) =>
    request<StrategyDetail>('/strategy/generate', {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('strategy'),
    }),

  version: (strategyId: string, versionId: string) =>
    request<StrategyVersion>(`/strategy/${strategyId}/versions/${versionId}`),

  createVersion: (strategyId: string, payload: Record<string, unknown>) =>
    request<StrategyDetail>(`/strategy/${strategyId}/versions`, {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('strategy-edit'),
    }),

  diff: (strategyId: string, left: number, right: number) =>
    request<VersionDiff>(`/strategy/${strategyId}/diff`, { query: { left, right } }),

  decide: (
    strategyId: string,
    payload: { action: string; reason?: string; version_id?: string | null },
  ) =>
    request<StrategyDetail>(`/strategy/${strategyId}/decision`, {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('strategy-decision'),
    }),

  executionStatus: (strategyId: string) =>
    request<{
      strategy_id: string
      status: string
      current_version_id: string | null
      current_version_number: number | null
      can_execute: boolean
      blocked_reason: string
      approved_version_id: string
      approval_id: string
      actions_available: string
    }>(`/strategy/${strategyId}/execution-status`),

  approvalHistory: (strategyId: string) =>
    request<StrategyDetail['approval_history']>(`/strategy/${strategyId}/approval-history`),
}

// ======================================================= strategy director flow
export const strategyDirectorApi = {
  estimate: (budgetUsd?: number) =>
    request<StrategyCapabilities['estimate_example']>('/director/strategy/estimate', {
      query: budgetUsd !== undefined ? { budget_usd: budgetUsd } : undefined,
    }),

  topology: () => request<StrategyCapabilities['topology']>('/director/strategy/topology'),

  start: (payload: {
    brand_profile_id: string
    objective: string
    business_objectives?: string[]
    primary_objective_type?: string
    platforms?: string[]
    research_project_id?: string | null
    budget_usd?: number | null
    max_iterations?: number | null
  }) =>
    request<StrategyWorkflowStartResponse>('/director/strategy/start', {
      method: 'POST',
      body: payload,
      idempotencyKey: newIdempotencyKey('strategy-workflow'),
    }),

  workflows: (limit = 50) =>
    request<Array<{ workflow_id: string; status: string; node: string; step: number | null }>>(
      '/director/strategy/workflows',
      { query: { limit } },
    ),

  status: (id: string) => request<StrategyWorkflowState>(`/director/strategy/workflows/${id}`),

  decide: (id: string, payload: { action: string; reason?: string }) =>
    request<{ workflow_id: string; state: StrategyWorkflowState }>(
      `/director/strategy/workflows/${id}/decision`,
      {
        method: 'POST',
        body: payload,
        idempotencyKey: newIdempotencyKey('strategy-workflow-decision'),
      },
    ),
}

// --------------------------------------------------- creative studio (3B)
export const creativeStudioApi = {
  overview: () => request<CreativeOverview>('/creative-studio/overview'),
  capabilities: () => request<CreativeCapabilities>('/creative-studio/capabilities'),
  flowStatus: () => request<CreativeFlowStatus>('/creative-studio/flow/status'),

  briefs: (query?: { content_item_id?: string; content_version_id?: string; limit?: number }) =>
    request<CreativeBrief[]>('/creative-studio/briefs', { query }),
  brief: (id: string) => request<CreativeBrief>(`/creative-studio/briefs/${id}`),
  createBrief: (
    payload: {
      content_version_id?: string
      content_item_id?: string
      asset_type?: 'image' | 'video'
      creative_direction?: string
      aspect_ratio?: string
      duration_seconds?: number
      strategy_reference?: string
      force_new_version?: boolean
    },
    idempotencyKey?: string,
  ) =>
    request<CreativeBrief>('/creative-studio/briefs', {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),

  jobs: (query?: { brief_id?: string; content_item_id?: string; status?: string; limit?: number }) =>
    request<CreativeJob[]>('/creative-studio/jobs', { query }),
  job: (id: string) => request<CreativeJob>(`/creative-studio/jobs/${id}`),
  submitJob: (
    payload: {
      brief_id: string
      job_type?: 'image' | 'video'
      parameters?: Record<string, unknown>
      auto_advance?: boolean
    },
    idempotencyKey?: string,
  ) =>
    request<CreativeJob>('/creative-studio/jobs', {
      method: 'POST',
      body: payload,
      idempotencyKey,
    }),
  pollJob: (id: string) =>
    request<CreativeJob>(`/creative-studio/jobs/${id}/poll`, { method: 'POST' }),
  retryJob: (id: string, authorizeAmbiguous = false) =>
    request<CreativeJob>(`/creative-studio/jobs/${id}/retry`, {
      method: 'POST',
      body: { authorize_ambiguous: authorizeAmbiguous },
    }),
  cancelJob: (id: string) =>
    request<CreativeJob>(`/creative-studio/jobs/${id}/cancel`, { method: 'POST' }),

  assets: (query?: {
    campaign_id?: string
    content_item_id?: string
    platform?: string
    kind?: string
    validation_status?: string
    q?: string
    limit?: number
  }) => request<CreativeAssetStudio[]>('/creative-studio/assets', { query }),
  asset: (id: string) => request<CreativeAssetDetail>(`/creative-studio/assets/${id}`),
  validateAsset: (id: string) =>
    request<{ asset: CreativeAssetStudio; qa_report: CreativeQaReport }>(
      `/creative-studio/assets/${id}/validate`,
      { method: 'POST' },
    ),
  selectAsset: (id: string, contentVersionId?: string) =>
    request<{
      asset: CreativeAssetStudio
      invalidated_approvals: number
      message: string
    }>(`/creative-studio/assets/${id}/select`, {
      method: 'POST',
      body: { content_version_id: contentVersionId ?? null },
    }),
  uploadAsset: (params: {
    file: File
    contentItemId?: string
    campaignId?: string
    kind?: string
  }): Promise<CreativeAssetStudio> => {
    const form = new FormData()
    form.append('file', params.file)
    if (params.contentItemId) form.append('content_item_id', params.contentItemId)
    if (params.campaignId) form.append('campaign_id', params.campaignId)
    if (params.kind) form.append('kind', params.kind)
    return request<CreativeAssetStudio>('/creative-studio/assets/upload', {
      method: 'POST',
      formData: form,
    })
  },
  uploadVersion: (assetId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{
      asset: CreativeAssetStudio
      invalidated_approvals: number
      message: string
    }>(`/creative-studio/assets/${assetId}/versions`, { method: 'POST', formData: form })
  },
  /** Authenticated URL for the raw bytes; the token travels as a query-free header
   *  via the app shell, so the UI uses `objectUrl` instead for <img> previews. */
  bytes: async (assetId: string): Promise<Blob> => {
    const token = getToken()
    const response = await fetch(`${API_BASE}/creative-studio/assets/${assetId}/bytes`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) {
      throw new ApiError(
        response.status,
        'asset_bytes_failed',
        `Could not load the stored bytes (${response.status}).`,
      )
    }
    return response.blob()
  },
}

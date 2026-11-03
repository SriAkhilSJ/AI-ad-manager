// Mirrors backend Pydantic schemas. Kept explicit (no codegen dependency) so the
// contract is reviewable in one file.

export type ContentStatus =
  | 'DRAFT'
  | 'IN_REVIEW'
  | 'APPROVED'
  | 'SCHEDULED'
  | 'PUBLISHING'
  | 'PUBLISHED'
  | 'REJECTED'
  | 'CHANGES_REQUESTED'
  | 'FAILED'
  | 'CANCELLED'

export type Platform =
  | 'instagram'
  | 'linkedin'
  | 'youtube'
  | 'x'
  | 'facebook'
  | 'reddit'

export const PLATFORMS: Platform[] = [
  'instagram',
  'linkedin',
  'youtube',
  'x',
  'facebook',
  'reddit',
]

export type Role = 'founder' | 'marketer' | 'viewer'

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details: Record<string, unknown>
    request_id: string
  }
}

export interface User {
  id: string
  email: string
  full_name: string
  role: Role
  is_active: boolean
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_at: string
  user: User
}

export interface BrandProfile {
  id: string
  name: string
  company: string
  product: string
  tagline: string
  positioning: string
  audience: string[]
  personality: string[]
  tone_of_voice: string[]
  visual_direction: Record<string, unknown>
  brand_values: string[]
  keywords: string[]
  do_not_claim: string[]
  prohibited_phrases: string[]
  compliance_notes: string
  version: number
  is_active: boolean
  created_at: string
  updated_at: string | null
}

export interface MarketingGoal {
  id: string
  brand_profile_id: string
  objective: string
  kpi: string
  target_value: number | null
  current_value: number | null
  horizon: string
  status: string
  priority: number
  due_on: string | null
  created_at: string
}

export interface ContentPillar {
  id: string
  brand_profile_id: string
  name: string
  description: string
  platforms: string[]
  weight: number
  is_active: boolean
  created_at: string
}

export interface Competitor {
  id: string
  brand_profile_id: string
  name: string
  website: string | null
  positioning: string
  strengths: string[]
  weaknesses: string[]
  source: string
  created_at: string
}

export interface ContentVersion {
  id: string
  content_item_id: string
  version_number: number
  headline: string
  body: string
  hashtags: string[]
  call_to_action: string
  media_refs: string[]
  content_hash: string
  change_note: string
  created_by: string | null
  created_by_agent: string | null
  generation_metadata: Record<string, unknown>
  created_at: string
}

export interface ContentItem {
  id: string
  brand_profile_id: string
  campaign_id: string | null
  pillar_id: string | null
  platform: Platform
  format: string
  title: string
  status: ContentStatus
  current_version_id: string | null
  version_count: number
  created_by_agent: string | null
  tags: string[]
  created_at: string
  updated_at: string | null
}

export interface ContentItemDetail extends ContentItem {
  current_version: ContentVersion | null
  allowed_actions: string[]
  has_valid_approval: boolean
  approval_count: number
}

export interface ApprovalRecord {
  id: string
  content_item_id: string
  content_version_id: string
  action: string
  actor_id: string | null
  actor_role: string
  reason: string
  content_hash: string
  invalidated_at: string | null
  invalidated_reason: string
  created_at: string
}

export interface Campaign {
  id: string
  brand_profile_id: string
  name: string
  objective: string
  brief: string
  status: string
  platforms: Platform[]
  start_date: string | null
  end_date: string | null
  plan: Record<string, unknown>
  created_by_agent: string | null
  created_at: string
  updated_at: string | null
}

export interface CampaignDetail extends Campaign {
  content_counts: Record<string, number>
  content_item_count: number
}

export interface CreativeAsset {
  id: string
  content_item_id: string | null
  content_version_id: string | null
  kind: string
  status: string
  filename: string
  mime_type: string
  size_bytes: number
  storage_key: string | null
  public_url: string | null
  checksum: string
  width: number | null
  height: number | null
  prompt: string
  provider: string
  metadata_json: Record<string, unknown>
  generated_by_agent: string | null
  created_at: string
}

export interface SocialAccount {
  id: string
  brand_profile_id: string
  platform: Platform
  handle: string
  display_name: string
  external_account_id: string | null
  status: string
  scopes: string[]
  capabilities: Record<string, unknown>
  token_expires_at: string | null
  last_synced_at: string | null
  notes: string
  created_at: string
}

export interface PlatformStatus {
  platform: Platform
  credentials_configured: boolean
  transport_available: boolean
  publish_enabled: boolean
  reason: string
}

export interface PlanTask {
  id: string
  agent: string
  action: string
  params: Record<string, unknown>
  depends_on: string[]
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
}

export interface WorkflowStatus {
  workflow_id: string
  status: string | null
  phase: string | null
  next: string[]
  awaiting_human: boolean
  interrupts: Array<{ value: unknown }>
  iteration: number | null
  plan: PlanTask[] | null
  qa_verdict: string | null
  qa_findings: ApprovalFinding[] | null
  approval_request: ApprovalRequest | null
  approval_decision: Record<string, unknown> | null
  content_item_ids: string[] | null
  created_campaign_id: string | null
  summary: string | null
  trace: Array<Record<string, unknown>> | null
  errors: Array<Record<string, unknown>> | null
  results: Record<string, unknown> | null
  checkpoint_count: number
}

export interface ApprovalFinding {
  platform?: string
  verdict?: string
  score?: number
  violations?: Array<{
    rule: string
    severity: string
    message: string
    evidence?: string
    fix?: string
  }>
  error?: string
}

export interface ApprovalRequest {
  workflow_id: string
  requested_at: string
  items: Array<{
    content_item_id: string
    title: string
    platform: string
    status: string
    content_version_id: string
    version_count: number
  }>
  qa_verdict: string | null
  qa_findings: ApprovalFinding[]
  summary: string
  decision_options: string[]
  note: string
}

export interface WorkflowSummary {
  workflow_id: string
  status: string | null
  node: string | null
  step: number | null
  updated_at: string | null
}

export interface AgentSpec {
  name: string
  description: string
  responsibilities: string[]
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  limits: { max_attempts: number; timeout_seconds: number; max_tokens: number }
}

export interface AgentRunSummary {
  id: string
  agent: string
  status: string
  workflow_thread_id: string | null
  workflow_node: string | null
  attempts: number
  latency_ms: number | null
  error_code: string
  created_at: string | null
  finished_at: string | null
}

export interface AnalyticsOverview {
  has_data: boolean
  data_source: string | string[]
  window_days: number
  platform: string | null
  message: string | null
  totals: {
    impressions: number
    reach: number
    engagements: number
    clicks: number
    likes: number
    comments: number
    shares: number
    saves: number
  }
  engagement_rate: number | null
  by_platform: Array<{
    platform: string
    impressions: number
    engagements: number
    clicks: number
    snapshots: number
  }>
  series: Array<{ date: string; impressions: number; engagements: number }>
  published_posts: number
}

export interface UsageSummary {
  has_data: boolean
  window_days: number
  message: string | null
  totals: {
    calls: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    cost_usd: number
  }
  by_provider: Array<{
    provider: string
    model: string
    calls: number
    total_tokens: number
    cost_usd: number
  }>
  by_agent: Array<{ agent: string; calls: number; total_tokens: number; cost_usd: number }>
  by_day: Array<{ date: string; total_tokens: number; cost_usd: number; calls: number }>
}

export interface SystemStatus {
  app: {
    name: string
    environment: string
    version: string
    api_prefix: string
    uptime_seconds: number
    bootstrap_required: boolean
  }
  database: {
    dialect: string
    status: string
    error: string | null
    pgvector: string
    vector_search: string
  }
  llm: {
    status: string
    providers: Record<string, { configured: boolean }>
    routing: { default: { provider: string | null; model: string | null }; configured: boolean }
    monthly_budget_usd: number
    note: string | null
  }
  tasks: {
    driver: string
    available: boolean
    status: string
    broker_configured: boolean
    requested: string
    note: string
  }
  storage: {
    backend: string
    available: boolean
    status: string
    configured: boolean
    root: string | null
    note: string | null
  }
  publishing: { status: string; mode: string; note: string }
  auth: { scheme: string; roles: string[]; approver_role: string }
  director: {
    orchestrator: string
    checkpointing: string
    human_in_the_loop: boolean
    nodes: number
  }
  rate_limit: { driver: string; per_minute: number }
}

export interface ScheduledPost {
  id: string
  content_item_id: string
  content_version_id: string
  social_account_id: string
  approval_record_id: string
  scheduled_for: string
  status: string
  attempts: number
  last_error: string
  created_at: string
}

export interface PublishedPost {
  id: string
  content_item_id: string
  content_version_id: string
  social_account_id: string
  approval_record_id: string
  platform: string
  external_post_id: string
  permalink: string | null
  published_at: string | null
  status: string
  created_at: string
}

export interface GraphTopology {
  entry: string
  nodes: string[]
  edges: string[][]
  interrupts: string[]
  checkpointer: string
}

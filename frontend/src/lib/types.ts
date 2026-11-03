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

// ==========================================================================
// Phase 2: research & strategy intelligence
// ==========================================================================

export type ResearchStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'budget_exceeded'
  | 'cancelled'
  | 'unavailable'

export type VerificationStatus =
  | 'verified'
  | 'unverified'
  | 'needs_confirmation'
  | 'disputed'

export interface ResearchProject {
  id: string
  brand_profile_id: string
  objective: string
  categories: string[]
  status: ResearchStatus
  summary: string
  provider: string
  max_cost_usd: number
  estimated_cost_usd: number
  actual_cost_usd: number
  tokens_used: number
  tool_calls: number
  iterations: number
  finding_count: number
  source_count: number
  gap_count: number
  error_code: string
  error_message: string
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface ResearchSource {
  id: string
  url: string
  title: string
  publisher: string
  credibility: string
  retrieved_at: string | null
  provider: string
  excerpt: string
}

export interface ResearchFinding {
  id: string
  research_project_id: string
  category: string
  finding: string
  evidence_summary: string
  source_url: string
  source_title: string
  retrieved_at: string | null
  confidence: number
  verification_status: VerificationStatus
  competitor_name: string
  tags: string[]
  provider: string
  created_at: string
}

export interface ResearchFindingPage {
  items: ResearchFinding[]
  total: number
  limit: number
  offset: number
}

export interface ResearchProjectDetail {
  project: ResearchProject
  findings: ResearchFinding[]
  sources: ResearchSource[]
  category_coverage: Array<{ category: string; findings: number; avg_confidence: number }>
  missing_categories: string[]
}

export interface ResearchRunResult {
  project: ResearchProject
  findings_created: number
  sources_created: number
  gaps: string[]
  warnings: string[]
  provider: string
  provider_status: string
  cost: Record<string, number | string>
  limits: Record<string, number | string | boolean>
  stop_reason: string
}

export interface ResearchCapabilities {
  provider: {
    provider: string
    status: 'available' | 'degraded' | 'unavailable'
    available: boolean
    network: boolean
    note: string
    max_sources?: number
  }
  categories: string[]
  verification_statuses: string[]
  max_sources: number
  budget_usd: number
  note: string
}

export interface CompetitorSnapshot {
  name: string
  website: string | null
  positioning: string | null
  source: string | null
  findings: number
}

export type StrategyStatus =
  | 'DRAFT'
  | 'IN_REVIEW'
  | 'APPROVED'
  | 'REJECTED'
  | 'CHANGES_REQUESTED'
  | 'SUPERSEDED'
  | 'ARCHIVED'

export interface Strategy {
  id: string
  brand_profile_id: string
  research_project_id: string | null
  campaign_id: string | null
  workflow_thread_id: string | null
  name: string
  objective: string
  status: StrategyStatus
  primary_objective_type: string
  version_count: number
  current_version_id: string | null
  created_by_agent: string | null
  created_at: string
  updated_at: string
}

export interface StrategySummary extends Strategy {
  has_valid_approval: boolean
  approved_version_id: string | null
  approved_at: string | null
  latest_version_number: number
  confidence: number
  business_objectives: string[]
}

export interface StrategyVersion {
  id: string
  strategy_id: string
  version_number: number
  positioning_recommendations: Array<Record<string, unknown>>
  audience_segmentation: Array<Record<string, unknown>>
  competitor_differentiation: Array<Record<string, unknown>>
  content_pillars: Array<Record<string, unknown>>
  campaign_concepts: Array<Record<string, unknown>>
  platform_strategies: Array<Record<string, unknown>>
  weekly_objectives: Array<Record<string, unknown>>
  publishing_frequency: Record<string, unknown>
  lead_generation: Array<Record<string, unknown>>
  growth_experiments: Array<Record<string, unknown>>
  kpis: Array<Record<string, unknown>>
  measurement_plan: Record<string, unknown>
  business_objectives: string[]
  objective_alignment: Record<string, unknown>
  source_finding_ids: string[]
  research_summary: string
  rationale: string
  brand_check: Record<string, unknown>
  missing_information: Array<Record<string, unknown>>
  warnings: string[]
  generation_mode: string
  confidence: number
  cost_usd: number
  tokens_used: number
  content_hash: string
  change_note: string
  created_by_agent: string | null
  created_at: string
}

export interface StrategyDetail {
  strategy: Strategy
  current_version: StrategyVersion | null
  versions: Array<{
    id: string
    version_number: number
    content_hash: string
    change_note: string
    confidence: number
    cost_usd: number
    generation_mode: string
    created_at: string | null
    business_objectives: string[]
  }>
  approval: {
    id: string
    strategy_version_id: string
    actor_id: string | null
    actor_role: string
    reason: string
    content_hash: string
    created_at: string | null
  } | null
  approval_history: Array<{
    id: string
    action: string
    strategy_version_id: string
    actor_role: string
    reason: string
    is_valid: boolean
    invalidated_at: string | null
    invalidated_reason: string
    created_at: string | null
  }>
  can_execute: boolean
  execution_blocked_reason: string
  experiments: Array<{
    id: string
    name: string
    hypothesis: string
    channel: string
    objective_type: string
    primary_metric: string
    expected_effort: string
    status: string
  }>
}

export interface StrategyCapabilities {
  business_objectives: string[]
  budget_usd: number
  budget_inr_equivalent: number
  max_iterations: number
  models: { reasoning: string | null; auxiliary: string | null }
  topology: GraphTopology
  estimate_example: CostEstimate
  note: string
}

export interface CostEstimate {
  estimated_cost_usd: number
  estimated_cost_inr: number
  budget_usd: number
  budget_inr: number
  within_budget: boolean
  research_calls: number
  strategy_calls: number
  models: { reasoning: string | null; auxiliary: string | null }
  assumptions: string[]
  note: string
}

export interface VersionDiff {
  left: { id: string; version_number: number }
  right: { id: string; version_number: number }
  changed_fields: string[]
  changes: Record<string, unknown>
  identical: boolean
}

export interface StrategyWorkflowState {
  workflow_id?: string
  status: string
  phase: string
  phase_label?: string
  objective?: string
  iteration?: number
  max_iterations?: number
  research_needed?: boolean
  research_rounds?: Array<{
    round: number
    categories_requested: string[]
    findings_created: number
    gaps: string[]
    provider: string
    stop_reason: string
    at: string
  }>
  findings_summary?: Record<string, unknown>
  evidence_verdict?: string | null
  evidence_assessment?: Record<string, unknown>
  brand_snapshot?: Record<string, unknown>
  unverified_brand_claims?: string[]
  strategy_id?: string | null
  strategy_version_id?: string | null
  strategy_version_number?: number
  strategy?: Record<string, unknown> | null
  brand_check?: Record<string, unknown>
  missing_information?: Array<{ area: string; detail: string; blocks_decision?: boolean }>
  approval_request?: Record<string, unknown> | null
  approval_decision?: Record<string, unknown> | null
  cost_estimate?: CostEstimate
  cost_actual?: Record<string, number>
  limits?: Record<string, number | boolean>
  events?: Array<Record<string, unknown>>
  trace?: Array<Record<string, unknown>>
  errors?: Array<Record<string, unknown>>
  summary?: string
  interrupted?: boolean
  awaiting_human?: boolean
  next?: string[]
  checkpoint_count?: number
}

export interface StrategyWorkflowStartResponse {
  workflow_id: string
  cost_estimate: CostEstimate
  state: StrategyWorkflowState
}

// ---------------------------------------------------------------- Phase 3A
export type ContentTypeName =
  | 'social_post'
  | 'caption'
  | 'hook'
  | 'short_form_video_script'
  | 'educational_post'
  | 'product_announcement'
  | 'founder_story'
  | 'thread'
  | 'carousel_outline'
  | 'community_discussion'
  | 'call_to_action'

export interface PlatformSpec {
  platform: string
  label: string
  supported_types: ContentTypeName[]
  body_max_chars: number
  title_max_chars: number
  hook_max_chars: number
  cta_max_chars: number
  hashtag_min: number
  hashtag_max: number
  hashtag_guidance: string
  formatting_rules: string[]
  cta_guidance: string
  validation_rules: string[]
  tone: string
  community_first: boolean
}

export interface StudioCapabilities {
  content_types: ContentTypeName[]
  platforms: string[]
  content_provider: string
  is_mock_provider: boolean
  live_model_calls_enabled: boolean
  external_publishing: false
  external_scheduling: false
  video_rendering: false
  notes: string[]
}

export interface StudioOverview {
  total_drafts: number
  pending_review: number
  approved: number
  scheduled_internally: number
  blocked: number
  by_platform: Record<string, number>
  by_campaign: Record<string, number>
  by_validation_status: Record<string, number>
  content_provider: string
  is_mock_provider: boolean
  live_model_calls_enabled: boolean
}

export type ValidationStatus = 'PASS' | 'PASS_WITH_WARNINGS' | 'NEEDS_REVISION' | 'BLOCKED'

export interface ValidationIssue {
  rule: string
  severity: 'blocking' | 'warning'
  category: string
  message: string
  explanation?: string
  evidence?: string
  affected_text?: string
  suggestion?: string
  requires_founder_confirmation?: boolean
}

export interface ValidationResult {
  verdict: ValidationStatus
  score: number
  issues: ValidationIssue[]
  checked_rules: string[]
  blocking_count: number
  warning_count: number
  requires_founder_confirmation: boolean
  provider: string
  generation_mode: string
  content_version_id: string
}

export interface ContentVariant {
  index: number
  headline: string
  hook: string
  body: string
  hashtags: string[]
  call_to_action: string
  outline?: string[]
  character_count: number
  provider: string
  model: string
  is_mock: boolean
  generation_mode: string
  score: number
  validation: {
    status: ValidationStatus
    verdict?: string
    score: number
    blocking_count: number
    warning_count: number
    requires_founder_confirmation?: boolean
    issues: ValidationIssue[]
    checked_rules?: string[]
  }
}

export interface VariantSet {
  content_item_id: string
  current_version_id: string | null
  version_number: number | null
  selected_variant: number | null
  variants: ContentVariant[]
  is_mock: boolean
  provider: string
  note: string
}

export interface GenerateRequest {
  campaign_id?: string | null
  platform: string
  content_type: ContentTypeName | string
  audience?: string
  pillar?: string
  objective?: string
  primary_message?: string
  variants?: number
  planned_date?: string
  planned_time?: string
}

export interface GeneratedItem {
  content_item_id: string
  version_id: string
  version_number: number
  platform: string
  content_type: string
  title: string
  status: string
  validation_status: ValidationStatus
  selected_variant: number
  variants: ContentVariant[]
  is_mock: boolean
  provider: string
  generation_metadata: Record<string, unknown>
}

export interface PlanConfig {
  posts_per_week: number
  platforms?: string[]
  content_mix?: Record<string, string[]>
  audience_focus?: string
  publishing_windows?: Record<string, string>
  variants?: number
  campaign_duration_weeks?: number
}

export interface PlanRequest {
  campaign_id?: string | null
  week_start?: string
  config?: PlanConfig
  generate?: boolean
  replace?: boolean
}

export interface PlanSlot {
  id: string
  position: number
  platform: string
  content_type: string
  scheduled_date: string
  scheduled_time: string
  audience: string
  pillar: string
  objective: string
  primary_message: string
  rationale: string
  status: string
  content_item_id: string | null
  variant_count: number
  selected_variant: number | null
  validation_status: ValidationStatus | null
  is_mock: boolean | null
}

export interface ContentPlan {
  id: string
  brand_profile_id: string
  campaign_id: string | null
  week_start: string
  week_end: string
  status: string
  objective: string
  platforms: string[]
  config: Record<string, unknown>
  slot_count: number
  item_count: number
  completeness_score: number
  generated_by: string
  generation_metadata: Record<string, unknown>
}

export interface ContentPlanDetail extends ContentPlan {
  slots: PlanSlot[]
}

export type CalendarLabel =
  | 'planned'
  | 'draft'
  | 'in_review'
  | 'approved'
  | 'changes_requested'
  | 'rejected'
  | 'scheduled_internally'
  | 'cancelled'
  | 'failed'

export interface CalendarEntry {
  entry_id: string
  content_item_id: string
  title: string
  platform: string
  content_type: string
  campaign_id: string | null
  plan_id: string | null
  status: string
  label: CalendarLabel
  planned_date: string
  planned_time: string
  note: string
  approval_required: boolean
  external_scheduling: false
  publishing_available: false
}

export interface CalendarView {
  view: 'month' | 'week' | 'list'
  start: string
  end: string
  entries: CalendarEntry[]
  external_scheduling: false
  publishing_available: false
}

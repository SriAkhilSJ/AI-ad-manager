import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { campaignApi, contentApi, contentStudioApi } from '../lib/api'
import type {
  ContentItem,
  ContentPlanDetail,
  ContentTypeName,
  GenerateRequest,
  GeneratedItem,
  PlatformSpec,
  StudioOverview,
} from '../lib/types'
import { PLATFORMS } from '../lib/types'
import { formatDate, titleCase } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Input,
  MetricCard,
  Select,
  Skeleton,
  SkeletonRows,
  StatusBadge,
  Tabs,
  Textarea,
  VerdictBadge,
} from '../components/ui'
import { PlatformChip } from '../components/shared'

type StudioTab = 'plan' | 'draft' | 'drafts'

const VERDICT_TONE: Record<string, 'ok' | 'warn' | 'danger' | 'muted'> = {
  PASS: 'ok',
  PASS_WITH_WARNINGS: 'warn',
  NEEDS_REVISION: 'warn',
  BLOCKED: 'danger',
}

function BlockedNotice({ blocked }: { blocked: boolean }) {
  if (!blocked) return null
  return (
    <div className="rounded-md border border-signal-danger/30 bg-signal-danger/10 px-3 py-2 text-[11px] leading-relaxed text-signal-danger">
      Blocked by the Brand Guardian. Unsupported claims, unverified statistics and fabricated
      testimonials must be removed before this draft can enter approval.
    </div>
  )
}

function IssueList({ validation }: { validation: GeneratedItem['variants'][number]['validation'] }) {
  if (!validation.issues.length) {
    return <p className="text-[11px] text-steel-600">No issues from the Brand Guardian.</p>
  }
  return (
    <ul className="space-y-2">
      {validation.issues.map((issue, index) => (
        <li key={`${issue.rule}-${index}`} className="rounded-md border border-white/[0.06] px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={issue.severity === 'blocking' ? 'danger' : 'warn'}>{issue.severity}</Badge>
            <span className="text-[11px] font-medium text-steel-400">{issue.rule}</span>
            {issue.requires_founder_confirmation ? (
              <Badge tone="violet">needs founder confirmation</Badge>
            ) : null}
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-steel-400">{issue.message}</p>
          {issue.explanation ? (
            <p className="mt-1 text-[11px] leading-relaxed text-steel-600">{issue.explanation}</p>
          ) : null}
          {issue.suggestion ? (
            <p className="mt-1 text-[11px] leading-relaxed text-steel-500">
              Suggested correction: {issue.suggestion}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

function OverviewPanel({ overview }: { overview: StudioOverview }) {
  const campaignCount = Object.keys(overview.by_campaign).length
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Drafts" value={overview.total_drafts} />
        <MetricCard label="Pending review" value={overview.pending_review} tone="warn" />
        <MetricCard label="Approved" value={overview.approved} tone="ok" />
        <MetricCard
          label="Planned (internal)"
          value={overview.scheduled_internally}
          hint="Calendar entries inside this app"
        />
        <MetricCard label="Blocked" value={overview.blocked} tone="warn" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Campaign distribution"
            subtitle={`${campaignCount} campaign${campaignCount === 1 ? '' : 's'} with drafts`}
          />
          {campaignCount === 0 ? (
            <EmptyState title="No drafts yet" description="Generate a plan to populate campaigns." />
          ) : (
            <ul className="divide-y divide-white/[0.05]">
              {Object.entries(overview.by_campaign).map(([campaignId, count]) => (
                <li key={campaignId} className="flex items-center justify-between py-2 text-xs">
                  <Link
                    to={`/campaigns/${campaignId}`}
                    className="truncate text-steel-400 hover:text-steel-300"
                  >
                    {campaignId.slice(0, 8)}
                  </Link>
                  <span className="tabular-nums text-steel-500">{count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader title="Platform distribution" subtitle="Where the drafts live" />
          {Object.keys(overview.by_platform).length === 0 ? (
            <EmptyState title="Nothing drafted" description="No platform has content yet." />
          ) : (
            <ul className="divide-y divide-white/[0.05]">
              {Object.entries(overview.by_platform).map(([platform, count]) => (
                <li key={platform} className="flex items-center justify-between py-2">
                  <PlatformChip platform={platform} />
                  <span className="text-xs tabular-nums text-steel-500">{count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Brand Guardian verdicts"
          subtitle="Latest validation recorded per draft"
        />
        {Object.keys(overview.by_validation_status).length === 0 ? (
          <EmptyState title="No validations" description="Validate a draft to record a verdict." />
        ) : (
          <div className="flex flex-wrap gap-2">
            {Object.entries(overview.by_validation_status).map(([verdict, count]) => (
              <Badge key={verdict} tone={VERDICT_TONE[verdict] ?? 'neutral'}>
                {verdict.replace(/_/g, ' ')} · {count}
              </Badge>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function PlanPanel({
  specs,
  onGenerated,
}: {
  specs: PlatformSpec[]
  onGenerated: () => void
}) {
  const campaigns = useApi(() => campaignApi.list())
  const [campaignId, setCampaignId] = useState('')
  const [weekStart, setWeekStart] = useState('')
  const [postsPerWeek, setPostsPerWeek] = useState(6)
  const [platforms, setPlatforms] = useState<string[]>([...PLATFORMS])
  const [variants, setVariants] = useState(1)
  const [plan, setPlan] = useState<ContentPlanDetail | null>(null)
  const [problem, setProblem] = useState<string | null>(null)

  const create = useMutation(async () => {
    setProblem(null)
    return contentStudioApi.createPlan({
      campaign_id: campaignId || null,
      week_start: weekStart || undefined,
      config: {
        posts_per_week: postsPerWeek,
        platforms,
        variants,
      },
      generate: true,
    })
  })

  const regenerate = useMutation(async () => {
    if (!plan) return null
    setProblem(null)
    return contentStudioApi.generatePlan(plan.id)
  })

  // Each selected platform can carry two slots in a given week before the
  // planner's round-robin starts repeating platforms.
  const capacity = useMemo(
    () => specs.filter((spec) => platforms.includes(spec.platform)).length * 2,
    [specs, platforms],
  )

  const submit = async () => {
    const result = await create.run(undefined as never)
    if (result) {
      setPlan(result)
      onGenerated()
    }
  }

  const runGeneration = async () => {
    const result = await regenerate.run(undefined as never)
    if (result) {
      setPlan(result)
      onGenerated()
    }
  }

  const overCapacity = postsPerWeek > capacity

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Weekly content plan"
          subtitle="The planner spreads slots across platforms, pillars and content types — not identical content everywhere."
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <Select
            label="Campaign"
            value={campaignId}
            onChange={(event) => setCampaignId(event.target.value)}
          >
            <option value="">No campaign</option>
            {(campaigns.data ?? []).map((campaign) => (
              <option key={campaign.id} value={campaign.id}>
                {campaign.name}
              </option>
            ))}
          </Select>
          <Input
            label="Week start (Monday)"
            type="date"
            value={weekStart}
            onChange={(event) => setWeekStart(event.target.value)}
            hint="Leave empty to use the next Monday."
          />
          <Input
            label="Posts this week"
            type="number"
            min={1}
            max={capacity}
            value={postsPerWeek}
            onChange={(event) => setPostsPerWeek(Number(event.target.value))}
            hint={`Planner capacity for the selected platforms: ${capacity}.`}
          />
          <Input
            label="Variants per slot"
            type="number"
            min={1}
            max={3}
            value={variants}
            onChange={(event) => setVariants(Number(event.target.value))}
          />
        </div>

        <div className="mt-3">
          <p className="hq-label mb-2">Active platforms</p>
          <div className="flex flex-wrap gap-1.5">
            {specs.map((spec) => {
              const active = platforms.includes(spec.platform)
              return (
                <button
                  key={spec.platform}
                  type="button"
                  onClick={() =>
                    setPlatforms((current) =>
                      active
                        ? current.filter((value) => value !== spec.platform)
                        : [...current, spec.platform],
                    )
                  }
                  className={
                    active
                      ? 'rounded-md border border-violet-500/40 bg-violet-500/10 px-2.5 py-1 text-[11px] text-violet-200'
                      : 'rounded-md border border-white/[0.08] px-2.5 py-1 text-[11px] text-steel-500 hover:text-steel-400'
                  }
                >
                  {spec.label}
                </button>
              )
            })}
          </div>
        </div>

        {overCapacity ? (
          <p className="mt-3 text-[11px] text-signal-warn">
            {postsPerWeek} posts exceeds the planner capacity of {capacity} for these platforms.
          </p>
        ) : null}
        {create.error ? <div className="mt-3"><ErrorState message={create.error.message} /></div> : null}
        {problem ? <p className="mt-3 text-[11px] text-signal-danger">{problem}</p> : null}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button onClick={submit} disabled={create.pending || overCapacity || !platforms.length}>
            {create.pending ? 'Planning…' : 'Create plan & draft the week'}
          </Button>
          {plan ? (
            <Button variant="ghost" onClick={runGeneration} disabled={regenerate.pending}>
              {regenerate.pending ? 'Generating…' : 'Fill remaining slots'}
            </Button>
          ) : null}
        </div>
      </Card>

      {plan ? (
        <Card>
          <CardHeader
            title={`Week of ${formatDate(plan.week_start)}`}
            subtitle={`${plan.item_count}/${plan.slot_count} slots drafted · completeness ${Math.round(
              plan.completeness_score * 100,
            )}% · generated by ${plan.generated_by}`}
            action={<Badge tone="violet">{titleCase(plan.status)}</Badge>}
          />
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wide text-steel-600">
                <tr>
                  <th className="py-2 pr-3 font-medium">Day</th>
                  <th className="py-2 pr-3 font-medium">Platform</th>
                  <th className="py-2 pr-3 font-medium">Type</th>
                  <th className="py-2 pr-3 font-medium">Pillar</th>
                  <th className="py-2 pr-3 font-medium">Rationale</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 font-medium">Draft</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.05]">
                {plan.slots.map((slot) => (
                  <tr key={slot.id}>
                    <td className="py-2 pr-3 text-steel-500">
                      {slot.scheduled_date} {slot.scheduled_time}
                    </td>
                    <td className="py-2 pr-3">
                      <PlatformChip platform={slot.platform} />
                    </td>
                    <td className="py-2 pr-3 text-steel-400">{titleCase(slot.content_type)}</td>
                    <td className="py-2 pr-3 text-steel-500">{slot.pillar || '—'}</td>
                    <td className="max-w-[280px] py-2 pr-3 text-[11px] text-steel-600">
                      {slot.rationale}
                    </td>
                    <td className="py-2 pr-3">
                      {slot.content_item_id ? (
                        <VerdictBadge verdict={slot.validation_status} />
                      ) : (
                        <Badge tone="muted">{titleCase(slot.status)}</Badge>
                      )}
                    </td>
                    <td className="py-2">
                      {slot.content_item_id ? (
                        <Link
                          to={`/content-studio/items/${slot.content_item_id}`}
                          className="text-[11px] text-violet-300 hover:text-violet-200"
                        >
                          Open
                        </Link>
                      ) : (
                        <span className="text-[11px] text-steel-700">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  )
}

function SingleDraftPanel({ specs, onGenerated }: { specs: PlatformSpec[]; onGenerated: () => void }) {
  const campaigns = useApi(() => campaignApi.list())
  const [platform, setPlatform] = useState(specs[0]?.platform ?? 'linkedin')
  const [contentType, setContentType] = useState<ContentTypeName>(
    (specs[0]?.supported_types[0] as ContentTypeName) ?? 'social_post',
  )
  const [campaignId, setCampaignId] = useState('')
  const [audience, setAudience] = useState('')
  const [pillar, setPillar] = useState('')
  const [message, setMessage] = useState('')
  const [variants, setVariants] = useState(2)
  const [result, setResult] = useState<GeneratedItem | null>(null)

  const spec = specs.find((item) => item.platform === platform)
  const supported = spec?.supported_types ?? []

  const generate = useMutation(async (payload: GenerateRequest) =>
    contentStudioApi.generate(payload),
  )

  const submit = async () => {
    if (supported.length && !supported.includes(contentType)) return
    const response = await generate.run({
      campaign_id: campaignId || null,
      platform,
      content_type: contentType,
      audience: audience || undefined,
      pillar: pillar || undefined,
      primary_message: message || undefined,
      variants,
    })
    if (response) {
      setResult(response)
      onGenerated()
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Generate a draft"
          subtitle="Each request produces distinct variants — different angles, not reworded copies."
        />
        <div className="grid gap-3 sm:grid-cols-2">
          <Select
            label="Platform"
            value={platform}
            onChange={(event) => {
              const next = event.target.value
              setPlatform(next)
              const nextSpec = specs.find((item) => item.platform === next)
              if (nextSpec && !nextSpec.supported_types.includes(contentType)) {
                setContentType(nextSpec.supported_types[0] as ContentTypeName)
              }
            }}
          >
            {specs.map((item) => (
              <option key={item.platform} value={item.platform}>
                {item.label}
              </option>
            ))}
          </Select>
          <Select
            label="Content type"
            value={contentType}
            onChange={(event) => setContentType(event.target.value as ContentTypeName)}
          >
            {supported.map((type) => (
              <option key={type} value={type}>
                {titleCase(type)}
              </option>
            ))}
          </Select>
          <Select
            label="Campaign"
            value={campaignId}
            onChange={(event) => setCampaignId(event.target.value)}
          >
            <option value="">No campaign</option>
            {(campaigns.data ?? []).map((campaign) => (
              <option key={campaign.id} value={campaign.id}>
                {campaign.name}
              </option>
            ))}
          </Select>
          <Input
            label="Audience"
            value={audience}
            onChange={(event) => setAudience(event.target.value)}
            placeholder="Software developers"
          />
          <Input
            label="Pillar"
            value={pillar}
            onChange={(event) => setPillar(event.target.value)}
            placeholder="Developer productivity"
          />
          <Input
            label="Variants"
            type="number"
            min={1}
            max={3}
            value={variants}
            onChange={(event) => setVariants(Number(event.target.value))}
          />
        </div>
        <div className="mt-3">
          <Textarea
            label="Primary message (optional)"
            rows={2}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Build and Ship with Ease."
          />
        </div>
        {spec ? (
          <p className="mt-3 text-[11px] leading-relaxed text-steel-600">
            {spec.label}: body ≤ {spec.body_max_chars} chars · {spec.hashtag_min}–{spec.hashtag_max}{' '}
            hashtags · {spec.cta_guidance}
          </p>
        ) : null}
        {generate.error ? <div className="mt-3"><ErrorState message={generate.error.message} /></div> : null}
        <div className="mt-4">
          <Button onClick={submit} disabled={generate.pending}>
            {generate.pending ? 'Generating…' : 'Generate variants'}
          </Button>
        </div>
      </Card>

      {result ? (
        <Card>
          <CardHeader
            title={result.title}
            subtitle={`v${result.version_number} · ${titleCase(result.platform)} · ${titleCase(
              result.content_type,
            )} · ${result.provider}`}
            action={
              <div className="flex items-center gap-2">
                <Badge tone={result.is_mock ? 'violet' : 'ok'}>
                  {result.is_mock ? 'mock provider' : result.provider}
                </Badge>
                <VerdictBadge verdict={result.validation_status} />
              </div>
            }
          />
          <div className="space-y-3">
            {result.variants.map((variant) => (
              <div key={variant.index} className="rounded-lg border border-white/[0.06] p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-steel-400">
                    Variant {variant.index + 1} · {variant.character_count} chars
                  </span>
                  <div className="flex items-center gap-2">
                    <Badge tone={VERDICT_TONE[variant.validation.status] ?? 'neutral'}>
                      {variant.validation.status.replace(/_/g, ' ')}
                    </Badge>
                    {variant.index === result.selected_variant ? (
                      <Badge tone="ok">selected</Badge>
                    ) : null}
                  </div>
                </div>
                <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-steel-300">
                  {variant.body}
                </p>
                {variant.hashtags.length ? (
                  <p className="mt-2 text-[11px] text-violet-300">
                    {variant.hashtags.map((tag) => `#${tag.replace(/^#/, '')}`).join(' ')}
                  </p>
                ) : null}
                <BlockedNotice blocked={variant.validation.status === 'BLOCKED'} />
                <div className="mt-2">
                  <IssueList validation={variant.validation} />
                </div>
              </div>
            ))}
          </div>
          <div className="mt-4">
            <Link
              to={`/content-studio/items/${result.content_item_id}`}
              className="text-[11px] text-violet-300 hover:text-violet-200"
            >
              Open in draft editor →
            </Link>
          </div>
        </Card>
      ) : null}
    </div>
  )
}

function DraftsPanel() {
  const [platform, setPlatform] = useState('')
  const [campaignId, setCampaignId] = useState('')
  const [status, setStatus] = useState('')
  const [pillar, setPillar] = useState('')
  const campaigns = useApi(() => campaignApi.list())
  const drafts = useApi(
    () =>
      contentApi.list({
        platform: platform || undefined,
        campaign_id: campaignId || undefined,
        status_filter: status || undefined,
        limit: 200,
      }),
    [platform, campaignId, status],
  )

  const filtered = useMemo(() => {
    const rows = drafts.data ?? []
    return pillar ? rows.filter((item) => (item.tags ?? []).includes(pillar)) : rows
  }, [drafts.data, pillar])

  if (drafts.loading) return <SkeletonRows rows={6} />
  if (drafts.error) return <ErrorState message={drafts.error.message} onRetry={drafts.reload} />
  if (!drafts.data?.length) {
    return (
      <EmptyState
        title="No drafts"
        description="Create a weekly plan or generate a single draft to get started."
      />
    )
  }

  return (
    <Card>
      <CardHeader
        title="Drafts"
        subtitle={`${filtered.length} matching draft${filtered.length === 1 ? '' : 's'}`}
      />
      <div className="mb-3 grid gap-2 sm:grid-cols-4">
        <Select label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">All platforms</option>
          {PLATFORMS.map((value) => (
            <option key={value} value={value}>
              {titleCase(value)}
            </option>
          ))}
        </Select>
        <Select
          label="Campaign"
          value={campaignId}
          onChange={(e) => setCampaignId(e.target.value)}
        >
          <option value="">All campaigns</option>
          {(campaigns.data ?? []).map((campaign) => (
            <option key={campaign.id} value={campaign.id}>
              {campaign.name}
            </option>
          ))}
        </Select>
        <Select label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {['DRAFT', 'IN_REVIEW', 'APPROVED', 'CHANGES_REQUESTED', 'REJECTED'].map((value) => (
            <option key={value} value={value}>
              {titleCase(value)}
            </option>
          ))}
        </Select>
        <Input
          label="Pillar tag"
          value={pillar}
          onChange={(e) => setPillar(e.target.value)}
          placeholder="Filter by tag"
        />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-steel-600">
            <tr>
              <th className="py-2 pr-3 font-medium">Title</th>
              <th className="py-2 pr-3 font-medium">Platform</th>
              <th className="py-2 pr-3 font-medium">Status</th>
              <th className="py-2 pr-3 font-medium">Versions</th>
              <th className="py-2 pr-3 font-medium">Updated</th>
              <th className="py-2 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.05]">
            {filtered.map((item: ContentItem) => (
              <tr key={item.id}>
                <td className="max-w-[320px] truncate py-2 pr-3 text-steel-300">{item.title}</td>
                <td className="py-2 pr-3">
                  <PlatformChip platform={item.platform} />
                </td>
                <td className="py-2 pr-3">
                  <StatusBadge status={item.status} />
                </td>
                <td className="py-2 pr-3 tabular-nums text-steel-500">{item.version_count}</td>
                <td className="py-2 pr-3 text-steel-600">
                  {formatDate(item.updated_at ?? item.created_at)}
                </td>
                <td className="py-2">
                  <Link
                    to={`/content-studio/items/${item.id}`}
                    className="text-[11px] text-violet-300 hover:text-violet-200"
                  >
                    Edit
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

export function ContentStudioPage() {
  const [tab, setTab] = useState<StudioTab>('plan')
  const capabilities = useApi(() => contentStudioApi.capabilities())
  const overview = useApi(() => contentStudioApi.overview())
  const platforms = useApi(() => contentStudioApi.platforms())

  const specs = platforms.data ?? []
  const reloadOverview = () => overview.reload()

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Content intelligence"
        title="Content Studio"
        description="Turn approved strategy into platform-specific drafts, validate them with the Brand Guardian and plan them on an internal calendar. Nothing is published or scheduled on a real social platform."
        actions={
          capabilities.data ? (
            <Badge tone={capabilities.data.is_mock_provider ? 'violet' : 'ok'} dot>
              {capabilities.data.is_mock_provider ? 'mock provider' : capabilities.data.content_provider}
            </Badge>
          ) : null
        }
      />

      {capabilities.data?.is_mock_provider ? (
        <Banner tone="warn" title="Deterministic mock provider">
          Drafts are composed from templates by the mock provider. No paid model call is made, and
          the text is not real AI output. Live model execution:{' '}
          {capabilities.data.live_model_calls_enabled ? 'enabled' : 'disabled'}.
        </Banner>
      ) : null}

      <Tabs
        tabs={[
          { id: 'plan', label: 'Weekly plan' },
          { id: 'draft', label: 'New draft' },
          { id: 'drafts', label: 'Drafts' },
        ]}
        value={tab}
        onChange={setTab}
      />

      {overview.loading ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {[0, 1, 2, 3, 4].map((key) => (
            <Skeleton key={key} className="h-20" />
          ))}
        </div>
      ) : overview.error ? (
        <ErrorState message={overview.error.message} onRetry={overview.reload} />
      ) : overview.data ? (
        <OverviewPanel overview={overview.data} />
      ) : null}

      {platforms.error ? <ErrorState message={platforms.error.message} onRetry={platforms.reload} /> : null}
      {platforms.loading ? (
        <SkeletonRows rows={3} />
      ) : tab === 'plan' ? (
        <PlanPanel specs={specs} onGenerated={reloadOverview} />
      ) : tab === 'draft' ? (
        <SingleDraftPanel specs={specs} onGenerated={reloadOverview} />
      ) : (
        <DraftsPanel />
      )}
    </div>
  )
}

import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { analyticsApi, brandApi, contentApi, directorApi, systemApi, socialApi } from '../lib/api'
import { formatNumber, formatPercent, formatUsd, relativeTime } from '../lib/format'
import { PageHeader, ErrorState } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  MetricCard,
  Mono,
  SectionTitle,
  Skeleton,
} from '../components/ui'
import { AgentChip, IntegrationRow, PlanView, PlatformChip } from '../components/shared'
import { IconArrowRight, IconDirector, IconLock } from '../components/layout/icons'

export function OverviewPage() {
  const brand = useApi(() => brandApi.active())
  const status = useApi(() => systemApi.status())
  const workflows = useApi(() => directorApi.workflows())
  const content = useApi(() => contentApi.list({ limit: 8 }))
  const analytics = useApi(() => analyticsApi.overview({ days: 30 }))
  const usage = useApi(() => analyticsApi.usage(30))
  const platforms = useApi(() => socialApi.platforms())

  const pendingApproval = (content.data ?? []).filter((item) =>
    ['IN_REVIEW', 'CHANGES_REQUESTED'].includes(item.status),
  )
  const approved = (content.data ?? []).filter((item) => item.status === 'APPROVED')
  const activeWorkflow = (workflows.data ?? [])[0]

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Marketing operations"
        title="Overview"
        description="Everything here is served from the live API. Nothing is simulated: when a number has no source, the panel says so."
        actions={
          <Link to="/director">
            <Button variant="primary">
              <IconDirector size={14} /> Open Director
            </Button>
          </Link>
        }
      />

      {/* brand strip */}
      {brand.error ? (
        <Banner
          tone="warn"
          title="No brand profile configured"
          action={
            <Link to="/brand">
              <Button size="sm">Configure brand</Button>
            </Link>
          }
        >
          The Marketing Director needs a brand knowledge base before it can plan. Seed the Crusor AI
          profile (<Mono>python -m app.seed.brand_crusor</Mono>) or create one in Brand Knowledge.
        </Banner>
      ) : brand.data ? (
        <Card className="flex flex-wrap items-center justify-between gap-4 px-5 py-4">
          <div className="flex min-w-0 items-center gap-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-white/[0.1] bg-gradient-to-br from-white/[0.1] to-transparent text-sm font-semibold text-steel-300">
              {brand.data.company.slice(0, 2).toUpperCase()}
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-steel-300">
                {brand.data.company} · {brand.data.product}
              </p>
              <p className="truncate text-[11px] text-steel-500">
                “{brand.data.tagline}” · profile v{brand.data.version}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {(brand.data.personality ?? []).slice(0, 3).map((trait) => (
              <Badge key={trait} tone="muted">
                {trait}
              </Badge>
            ))}
            <Badge tone="violet">{brand.data.do_not_claim.length} claim guards</Badge>
          </div>
        </Card>
      ) : (
        <Skeleton className="h-[68px] w-full" />
      )}

      {/* metrics */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Awaiting approval"
          value={content.loading ? '—' : formatNumber(pendingApproval.length)}
          hint="In review or changes requested"
          tone={pendingApproval.length ? 'warn' : 'neutral'}
        />
        <MetricCard
          label="Approved, not published"
          value={content.loading ? '—' : formatNumber(approved.length)}
          hint="Publishing is a separate gated action"
          tone="violet"
        />
        <MetricCard
          label="Model spend (30d)"
          value={
            usage.data?.has_data ? formatUsd(usage.data.totals.cost_usd) : '—'
          }
          hint={
            usage.data?.has_data
              ? `${formatNumber(usage.data.totals.total_tokens)} tokens`
              : 'No provider configured — zero recorded usage'
          }
          unavailable={!usage.data?.has_data}
        />
        <MetricCard
          label="Engagement rate"
          value={
            analytics.data?.has_data ? formatPercent(analytics.data.engagement_rate) : '—'
          }
          hint={
            analytics.data?.has_data
              ? `${formatNumber(analytics.data.totals.impressions)} impressions`
              : 'No analytics snapshots recorded'
          }
          unavailable={!analytics.data?.has_data}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        {/* director */}
        <Card className="xl:col-span-2">
          <CardHeader
            title="Marketing Director"
            subtitle="LangGraph supervisor with database checkpointing and a human approval interrupt."
            action={
              <Link to="/director">
                <Button size="sm" variant="ghost">
                  Manage <IconArrowRight size={13} />
                </Button>
              </Link>
            }
          />
          <div className="px-5 py-4">
            {workflows.loading ? (
              <Skeleton className="h-24 w-full" />
            ) : workflows.error ? (
              <ErrorState message={workflows.error.message} onRetry={workflows.reload} />
            ) : activeWorkflow ? (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <Mono>{activeWorkflow.workflow_id}</Mono>
                    <p className="mt-1 text-xs text-steel-500">
                      node <span className="text-steel-300">{activeWorkflow.node ?? '—'}</span> ·
                      step {activeWorkflow.step ?? '—'} · {relativeTime(activeWorkflow.updated_at)}
                    </p>
                  </div>
                  <Badge
                    tone={
                      activeWorkflow.status === 'awaiting_approval'
                        ? 'warn'
                        : activeWorkflow.status === 'completed'
                          ? 'ok'
                          : 'info'
                    }
                    dot
                  >
                    {activeWorkflow.status ?? 'unknown'}
                  </Badge>
                </div>
                <PlanView plan={null} />
              </div>
            ) : (
              <EmptyState
                icon={<IconDirector />}
                title="No workflows yet"
                description="Start a workflow from the Director page: it researches, plans, drafts, runs QA and then stops for your approval."
                action={
                  <Link to="/director">
                    <Button variant="primary" size="sm">
                      Start a workflow
                    </Button>
                  </Link>
                }
              />
            )}
          </div>
        </Card>

        {/* integrity */}
        <Card>
          <CardHeader title="Control plane" subtitle="What the system will and will not do." />
          <div className="px-5 py-3">
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-violet-500/20 bg-violet-500/[0.06] px-3 py-2">
              <IconLock size={13} className="text-violet-300" />
              <p className="text-[11px] leading-relaxed text-violet-200/90">
                Human approval required. The AI can research, plan and create — it cannot publish
                without a founder approval bound to an exact content version.
              </p>
            </div>
            {status.data ? (
              <>
                <IntegrationRow
                  name="Database"
                  status={status.data.database.status}
                  note={`${status.data.database.dialect} · vector search: ${status.data.database.vector_search}`}
                />
                <IntegrationRow
                  name="LLM gateway"
                  status={status.data.llm.status}
                  configured={status.data.llm.routing.configured}
                  note={status.data.llm.note}
                />
                <IntegrationRow
                  name="Background tasks"
                  status={status.data.tasks.status}
                  note={status.data.tasks.note}
                />
                <IntegrationRow
                  name="Object storage"
                  status={status.data.storage.status}
                  note={status.data.storage.note}
                />
                <IntegrationRow
                  name="Social publishing"
                  status={status.data.publishing.status}
                  note={status.data.publishing.note}
                />
              </>
            ) : (
              <div className="space-y-2">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
              </div>
            )}
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {/* content queue */}
        <Card>
          <CardHeader
            title="Content queue"
            subtitle="Latest items across all platforms."
            action={
              <Link to="/calendar">
                <Button size="sm" variant="ghost">
                  Calendar <IconArrowRight size={13} />
                </Button>
              </Link>
            }
          />
          {content.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : content.error ? (
            <div className="p-4">
              <ErrorState message={content.error.message} onRetry={content.reload} />
            </div>
          ) : (content.data ?? []).length === 0 ? (
            <EmptyState
              compact
              title="No content yet"
              description="Content appears here once the Director drafts it or you create an item."
            />
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {(content.data ?? []).map((item) => (
                <li key={item.id}>
                  <Link
                    to={`/content/${item.id}`}
                    className="flex items-center justify-between gap-3 px-5 py-3 transition-colors hover:bg-white/[0.02]"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-xs text-steel-300">{item.title}</p>
                      <div className="mt-1 flex items-center gap-2">
                        <PlatformChip platform={item.platform} />
                        {item.created_by_agent ? (
                          <AgentChip agent={item.created_by_agent} size="xs" />
                        ) : null}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="hq-mono">v{item.version_count}</span>
                      <Badge tone={item.status === 'APPROVED' ? 'ok' : 'muted'}>
                        {item.status}
                      </Badge>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* channels + analytics honesty */}
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Channel readiness"
              subtitle="Publish is blocked until a platform transport exists."
              action={
                <Link to="/social">
                  <Button size="sm" variant="ghost">
                    Accounts <IconArrowRight size={13} />
                  </Button>
                </Link>
              }
            />
            <div className="grid gap-2 px-5 py-4 sm:grid-cols-2">
              {(platforms.data ?? []).map((row) => (
                <div
                  key={row.platform}
                  className="flex items-center justify-between gap-2 rounded-lg border border-white/[0.06] bg-white/[0.02] px-2.5 py-2"
                >
                  <PlatformChip platform={row.platform} />
                  <Badge tone={row.publish_enabled ? 'ok' : 'muted'}>
                    {row.publish_enabled ? 'ready' : 'gated'}
                  </Badge>
                </div>
              ))}
              {!platforms.data ? <Skeleton className="h-9 w-full" /> : null}
            </div>
          </Card>

          <Card>
            <CardHeader title="Performance data" subtitle="Provenance, not estimates." />
            <div className="px-5 py-4">
              {analytics.data && !analytics.data.has_data ? (
                <div className="text-[11px] leading-relaxed text-steel-500">
                  <Badge tone="muted">no snapshots</Badge>
                  <p className="mt-2">{analytics.data.message}</p>
                </div>
              ) : analytics.data ? (
                <div className="space-y-1">
                  <SectionTitle>Totals · last {analytics.data.window_days} days</SectionTitle>
                  <div className="grid grid-cols-2 gap-x-4 text-xs text-steel-400">
                    <span>Impressions</span>
                    <span className="text-right text-steel-300">
                      {formatNumber(analytics.data.totals.impressions)}
                    </span>
                    <span>Engagements</span>
                    <span className="text-right text-steel-300">
                      {formatNumber(analytics.data.totals.engagements)}
                    </span>
                    <span>Clicks</span>
                    <span className="text-right text-steel-300">
                      {formatNumber(analytics.data.totals.clicks)}
                    </span>
                  </div>
                </div>
              ) : (
                <Skeleton className="h-16 w-full" />
              )}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}

import { Link, useParams } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { campaignApi, contentApi } from '../lib/api'
import { formatDate, relativeTime, titleCase } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  SectionTitle,
  Skeleton,
  StatusBadge,
} from '../components/ui'
import { AgentChip, PlatformChip, StatRow } from '../components/shared'

export function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>()
  const campaign = useApi(() => campaignApi.get(id as string), [id], { enabled: Boolean(id) })
  const content = useApi(
    () => contentApi.list({ campaign_id: id, limit: 100 }),
    [id],
    { enabled: Boolean(id) },
  )

  if (campaign.loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (campaign.error || !campaign.data) {
    return (
      <div className="space-y-4">
        <Link to="/campaigns" className="text-[11px] text-steel-500 hover:text-steel-300">
          ← Campaigns
        </Link>
        <ErrorState
          title="Campaign unavailable"
          message={campaign.error?.message ?? 'Not found.'}
          onRetry={campaign.reload}
        />
      </div>
    )
  }

  const data = campaign.data
  const plan = (data.plan ?? {}) as Record<string, unknown>
  const pillars = (plan.messaging_pillars as string[] | undefined) ?? []
  const metrics = (plan.success_metrics as string[] | undefined) ?? []
  const slots = (plan.content_plan as Array<Record<string, unknown>> | undefined) ?? []

  return (
    <div className="space-y-5">
      <Link to="/campaigns" className="text-[11px] text-steel-500 hover:text-steel-300">
        ← Campaigns
      </Link>

      <PageHeader
        eyebrow={titleCase(data.status)}
        title={data.name}
        description={data.objective || 'No objective recorded for this campaign.'}
        actions={
          <div className="flex flex-wrap gap-1.5">
            {data.platforms.map((platform) => (
              <PlatformChip key={platform} platform={platform} />
            ))}
          </div>
        }
      />

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader
            title="Strategy plan"
            subtitle={
              data.created_by_agent
                ? `Produced by the ${data.created_by_agent} agent`
                : 'Created manually — no strategy plan recorded'
            }
            action={data.created_by_agent ? <AgentChip agent={data.created_by_agent} /> : null}
          />
          <div className="px-5 py-4">
            {pillars.length === 0 && slots.length === 0 ? (
              <EmptyState
                compact
                title="No structured plan"
                description="Run a Director workflow to generate research-backed messaging pillars, channel mix and content slots."
              />
            ) : (
              <div className="space-y-5">
                {pillars.length > 0 ? (
                  <div>
                    <SectionTitle>Messaging pillars</SectionTitle>
                    <div className="flex flex-wrap gap-1.5">
                      {pillars.map((pillar) => (
                        <Badge key={pillar} tone="violet">
                          {pillar}
                        </Badge>
                      ))}
                    </div>
                  </div>
                ) : null}

                {slots.length > 0 ? (
                  <div>
                    <SectionTitle>Content slots</SectionTitle>
                    <div className="overflow-hidden rounded-lg border border-white/[0.06]">
                      <table className="hq-table">
                        <thead>
                          <tr>
                            <th>Platform</th>
                            <th>Format</th>
                            <th>Pillar</th>
                            <th>Angle</th>
                          </tr>
                        </thead>
                        <tbody>
                          {slots.map((slot, index) => (
                            <tr key={index}>
                              <td>
                                <PlatformChip platform={String(slot.platform ?? '')} />
                              </td>
                              <td className="hq-mono">{String(slot.format ?? '—')}</td>
                              <td>{String(slot.pillar ?? '—')}</td>
                              <td className="text-steel-400">{String(slot.angle ?? '—')}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ) : null}

                {metrics.length > 0 ? (
                  <div>
                    <SectionTitle>Success metrics</SectionTitle>
                    <ul className="space-y-1">
                      {metrics.map((metric) => (
                        <li key={metric} className="text-[11px] text-steel-400">
                          · {metric}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        </Card>

        <Card className="self-start">
          <CardHeader title="Campaign facts" />
          <div className="px-5 py-3">
            <StatRow label="Status" value={<Badge tone="muted">{titleCase(data.status)}</Badge>} />
            <StatRow label="Created" value={formatDate(data.created_at)} />
            <StatRow label="Start" value={formatDate(data.start_date)} />
            <StatRow label="End" value={formatDate(data.end_date)} />
            <StatRow label="Content items" value={data.content_item_count} />
            <div className="mt-3 border-t border-white/[0.05] pt-3">
              <SectionTitle>By status</SectionTitle>
              {Object.keys(data.content_counts ?? {}).length === 0 ? (
                <p className="text-[11px] text-steel-600">No content attached.</p>
              ) : (
                Object.entries(data.content_counts).map(([status, count]) => (
                  <StatRow key={status} label={titleCase(status)} value={count} />
                ))
              )}
            </div>
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Content items"
          subtitle={content.data ? `${content.data.length} item(s)` : undefined}
        />
        {content.loading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : content.error ? (
          <div className="p-4">
            <ErrorState message={content.error.message} onRetry={content.reload} />
          </div>
        ) : (content.data ?? []).length === 0 ? (
          <EmptyState
            compact
            title="No content in this campaign"
            description="Content items created by the Director for this campaign appear here."
          />
        ) : (
          <table className="hq-table">
            <thead>
              <tr>
                <th>Title</th>
                <th>Platform</th>
                <th>Status</th>
                <th>Version</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {(content.data ?? []).map((item) => (
                <tr key={item.id}>
                  <td>
                    <Link
                      to={`/content/${item.id}`}
                      className="text-steel-300 transition-colors hover:text-violet-300"
                    >
                      {item.title}
                    </Link>
                  </td>
                  <td>
                    <PlatformChip platform={item.platform} />
                  </td>
                  <td>
                    <StatusBadge status={item.status} />
                  </td>
                  <td>
                    <Mono>v{item.version_count}</Mono>
                  </td>
                  <td className="text-steel-500">{relativeTime(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}

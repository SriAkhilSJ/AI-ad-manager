import { useApi } from '../../hooks/useApi'
import { creativeStudioApi } from '../../lib/api'
import { ErrorState } from '../../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Card,
  CardHeader,
  EmptyState,
  KeyValue,
  MetricCard,
  Mono,
  SectionTitle,
  Skeleton,
} from '../../components/ui'

/**
 * Overview of the creative subsystem: what exists, what is blocked, and what the
 * provider can actually do. Counts come from the API — nothing is estimated here.
 */
export function CreativeOverviewPanel() {
  const overview = useApi(() => creativeStudioApi.overview())
  const flow = useApi(() => creativeStudioApi.flowStatus())

  if (overview.error) {
    return <ErrorState message={overview.error.message} onRetry={overview.reload} />
  }
  if (overview.loading || !overview.data) {
    return (
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  const data = overview.data
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Assets"
          value={data.total_assets}
          hint={`${data.mock_assets} mock · ${data.needs_review_assets} need review`}
        />
        <MetricCard
          label="Jobs"
          value={data.succeeded_jobs}
          hint={`${data.pending_jobs} in flight · ${data.failed_jobs} failed`}
          tone={data.failed_jobs > 0 ? 'warn' : 'neutral'}
        />
        <MetricCard
          label="Approved assets"
          value={data.approved_assets}
          hint="Approvals bind content version + brief version + asset version"
          tone="violet"
        />
        <MetricCard
          label="Provider"
          value={<span className="text-lg">{data.provider}</span>}
          hint={data.is_mock_provider ? 'mock — zero cost, synthetic frames' : 'live provider'}
          tone={data.is_mock_provider ? 'warn' : 'neutral'}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="By platform" subtitle="Assets currently in the studio" />
          <div className="px-5 py-4">
            {Object.keys(data.by_platform).length === 0 ? (
              <EmptyState
                compact
                title="No assets yet"
                description="Generate a brief, then submit a mock job from the Generation centre."
              />
            ) : (
              <div className="space-y-2">
                {Object.entries(data.by_platform).map(([platform, count]) => (
                  <div key={platform} className="flex items-center justify-between gap-3">
                    <Badge tone="muted">{platform}</Badge>
                    <span className="hq-mono text-xs text-steel-400">{count}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>

        <Card className="self-start">
          <CardHeader title="Validation" subtitle="Creative QA verdicts" />
          <div className="px-5 py-4">
            {Object.keys(data.by_validation).length === 0 ? (
              <p className="text-xs text-steel-500">No assets validated yet.</p>
            ) : (
              <div className="space-y-2">
                {Object.entries(data.by_validation).map(([status, count]) => (
                  <div key={status} className="flex items-center justify-between gap-3">
                    <Badge tone={status === 'pass' ? 'ok' : status === 'blocked' ? 'danger' : 'warn'}>
                      {status.replace(/_/g, ' ')}
                    </Badge>
                    <span className="hq-mono text-xs text-steel-400">{count}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Generation provider"
            subtitle="Read from the live configuration, not from a mock-up"
          />
          <div className="space-y-2 px-5 py-4">
            <KeyValue label="Provider">
              <Mono>{data.provider}</Mono>
            </KeyValue>
            <KeyValue label="Mode">
              <Badge tone={data.is_mock_provider ? 'warn' : 'ok'}>
                {data.is_mock_provider ? 'mock — synthetic' : 'live'}
              </Badge>
            </KeyValue>
            <KeyValue label="Paid generation">
              <Badge tone={data.paid_generation_enabled ? 'danger' : 'ok'}>
                {data.paid_generation_enabled ? 'authorised' : 'disabled'}
              </Badge>
            </KeyValue>
            <KeyValue label="Storage">
              <Mono>{String(data.storage.backend ?? 'unknown')}</Mono>
            </KeyValue>
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Flow Connector"
            subtitle="Kept disabled — the integration cannot be implemented from evidence"
            action={
              <Badge tone="muted">{flow.data ? flow.data.status.replace(/_/g, ' ') : '…'}</Badge>
            }
          />
          <div className="px-5 py-4">
            {flow.error ? (
              <ErrorState message={flow.error.message} onRetry={flow.reload} />
            ) : flow.loading || !flow.data ? (
              <Skeleton className="h-20 w-full" />
            ) : (
              <div className="space-y-3">
                <p className="text-xs leading-relaxed text-steel-400">{flow.data.note}</p>
                <SectionTitle>Still required</SectionTitle>
                <ul className="list-disc space-y-1 pl-4 text-[11px] leading-relaxed text-steel-400">
                  {flow.data.required_information.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Card>
      </div>

      <Banner tone="neutral">
        Automated Creative QA inspects integrity, format, palette and claim language. It performs no
        OCR and makes no aesthetic judgement, so every asset still needs a human look before it is
        used anywhere.
      </Banner>
    </div>
  )
}

import { useState } from 'react'
import { useApi } from '../hooks/useApi'
import { analyticsApi } from '../lib/api'
import { formatNumber, formatPercent, formatUsd, relativeTime } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
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
  Tabs,
} from '../components/ui'
import { AgentChip, Sparkline } from '../components/shared'
import { IconAnalytics } from '../components/layout/icons'

type Tab = 'performance' | 'usage' | 'runs'

export function AnalyticsPage() {
  const [tab, setTab] = useState<Tab>('performance')
  const [days, setDays] = useState(30)

  const performance = useApi(() => analyticsApi.overview({ days }), [days], {
    enabled: tab === 'performance',
  })
  const usage = useApi(() => analyticsApi.usage(days), [days], { enabled: tab === 'usage' })
  const runs = useApi(() => analyticsApi.agentRuns(50), [], { enabled: tab === 'runs' })

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Measurement"
        title="Analytics"
        description="Two separate truths: what the audience did (needs platform connectors) and what the agents cost (measured from the LLM gateway). Neither is estimated."
        actions={
          <div className="flex items-center gap-2">
            <Tabs<Tab>
              value={tab}
              onChange={setTab}
              tabs={[
                { id: 'performance', label: 'Performance' },
                { id: 'usage', label: 'Model usage' },
                { id: 'runs', label: 'Agent runs' },
              ]}
            />
            <select
              className="hq-input h-8 w-auto py-1 text-[11px]"
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
            >
              <option value={7}>7 days</option>
              <option value={30}>30 days</option>
              <option value={90}>90 days</option>
            </select>
          </div>
        }
      />

      {tab === 'performance' ? (
        <PerformanceTab state={performance} />
      ) : tab === 'usage' ? (
        <UsageTab state={usage} />
      ) : (
        <RunsTab state={runs} />
      )}
    </div>
  )
}

function PerformanceTab({
  state,
}: {
  state: ReturnType<typeof useApi<Awaited<ReturnType<typeof analyticsApi.overview>>>>
}) {
  if (state.loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }
  if (state.error) return <ErrorState message={state.error.message} onRetry={state.reload} />
  if (!state.data) return null

  const data = state.data
  if (!data.has_data) {
    return (
      <Card>
        <EmptyState
          icon={<IconAnalytics />}
          title="No performance data recorded"
          description={data.message ?? 'Connect a platform connector to collect metrics.'}
          action={
            <Button size="sm" variant="secondary" onClick={state.reload}>
              Re-check
            </Button>
          }
        />
        <div className="border-t border-white/[0.06] px-5 py-4">
          <SectionTitle>What would produce data</SectionTitle>
          <ul className="space-y-1.5 text-[11px] text-steel-500">
            <li>· A completed OAuth connection for at least one social account.</li>
            <li>· A published post, since snapshots are keyed to publications.</li>
            <li>· The Phase 2 analytics collector writing <Mono>analytics_snapshots</Mono> rows.</li>
          </ul>
          <p className="mt-3 text-[11px] text-steel-600">
            Published posts recorded so far: {data.published_posts}
          </p>
        </div>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Impressions" value={formatNumber(data.totals.impressions)} tone="violet" />
        <MetricCard label="Engagements" value={formatNumber(data.totals.engagements)} />
        <MetricCard label="Clicks" value={formatNumber(data.totals.clicks)} />
        <MetricCard
          label="Engagement rate"
          value={formatPercent(data.engagement_rate)}
          hint={`${data.totals.reach} reach`}
        />
      </div>

      <Card>
        <CardHeader
          title="Daily impressions"
          subtitle={`Window: ${data.window_days} days · source: ${String(data.data_source)}`}
        />
        <div className="px-5 py-4">
          <Sparkline points={data.series.map((point) => point.impressions)} />
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {data.series.slice(-6).map((point) => (
              <div
                key={point.date}
                className="flex items-center justify-between rounded-md border border-white/[0.05] px-2.5 py-1.5"
              >
                <span className="hq-mono">{point.date}</span>
                <span className="text-[11px] text-steel-300">
                  {formatNumber(point.impressions)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader title="By platform" />
        <table className="hq-table">
          <thead>
            <tr>
              <th>Platform</th>
              <th>Impressions</th>
              <th>Engagements</th>
              <th>Clicks</th>
              <th>Snapshots</th>
            </tr>
          </thead>
          <tbody>
            {data.by_platform.map((row) => (
              <tr key={row.platform}>
                <td className="capitalize">{row.platform}</td>
                <td className="tabular-nums">{formatNumber(row.impressions)}</td>
                <td className="tabular-nums">{formatNumber(row.engagements)}</td>
                <td className="tabular-nums">{formatNumber(row.clicks)}</td>
                <td className="hq-mono">{row.snapshots}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}

function UsageTab({
  state,
}: {
  state: ReturnType<typeof useApi<Awaited<ReturnType<typeof analyticsApi.usage>>>>
}) {
  if (state.loading) {
    return <Skeleton className="h-64 w-full" />
  }
  if (state.error) return <ErrorState message={state.error.message} onRetry={state.reload} />
  if (!state.data) return null
  const data = state.data

  if (!data.has_data) {
    return (
      <Card>
        <EmptyState
          title="No model usage recorded"
          description={data.message ?? 'Configure an LLM provider to route agent calls through the gateway.'}
        />
        <div className="border-t border-white/[0.06] px-5 py-4">
          <Banner tone="neutral" title="Why this is not a bug">
            Agents run deterministic implementations when no provider is configured. They do real
            work (planning, copy templates, brand checks) and record zero token usage, because no
            tokens were consumed. Cost reporting stays at $0.00 rather than inventing spend.
          </Banner>
        </div>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Cost" value={formatUsd(data.totals.cost_usd)} tone="violet" />
        <MetricCard label="Total tokens" value={formatNumber(data.totals.total_tokens)} />
        <MetricCard label="Prompt tokens" value={formatNumber(data.totals.prompt_tokens)} />
        <MetricCard label="Calls" value={formatNumber(data.totals.calls)} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title="By provider / model" />
          <table className="hq-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Calls</th>
                <th>Tokens</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.by_provider.map((row) => (
                <tr key={`${row.provider}-${row.model}`}>
                  <td>
                    <span className="text-steel-300">{row.model}</span>
                    <span className="ml-1.5 hq-mono">{row.provider}</span>
                  </td>
                  <td className="tabular-nums">{row.calls}</td>
                  <td className="tabular-nums">{formatNumber(row.total_tokens)}</td>
                  <td className="tabular-nums">{formatUsd(row.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card>
          <CardHeader title="By agent" />
          <ul className="divide-y divide-white/[0.04]">
            {data.by_agent.map((row) => (
              <li key={row.agent} className="flex items-center justify-between gap-3 px-5 py-2.5">
                <AgentChip agent={row.agent} />
                <div className="text-right">
                  <p className="text-xs tabular-nums text-steel-300">
                    {formatNumber(row.total_tokens)} tokens
                  </p>
                  <p className="hq-mono">{formatUsd(row.cost_usd)}</p>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      {data.by_day.length > 0 ? (
        <Card>
          <CardHeader title="Daily cost" subtitle="Every row is a recorded gateway call." />
          <div className="px-5 py-4">
            <Sparkline points={data.by_day.map((point) => point.cost_usd * 1000)} />
          </div>
        </Card>
      ) : null}
    </div>
  )
}

function RunsTab({
  state,
}: {
  state: ReturnType<typeof useApi<Awaited<ReturnType<typeof analyticsApi.agentRuns>>>>
}) {
  if (state.loading) return <Skeleton className="h-64 w-full" />
  if (state.error) return <ErrorState message={state.error.message} onRetry={state.reload} />
  if (!state.data || state.data.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No agent runs yet"
          description="Every agent invocation is written to this ledger with attempts, latency and trace."
        />
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader
        title="Agent execution ledger"
        subtitle={`${state.data.length} recent run(s)`}
        action={
          <Button size="sm" variant="ghost" onClick={state.reload}>
            Refresh
          </Button>
        }
      />
      <table className="hq-table">
        <thead>
          <tr>
            <th>Agent</th>
            <th>Status</th>
            <th>Attempts</th>
            <th>Latency</th>
            <th>Workflow</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {state.data.map((run) => (
            <tr key={run.id}>
              <td>
                <AgentChip agent={run.agent} size="xs" />
              </td>
              <td>
                <Badge
                  tone={
                    run.status === 'succeeded'
                      ? 'ok'
                      : run.status === 'failed'
                        ? 'danger'
                        : 'info'
                  }
                  dot
                >
                  {run.status}
                </Badge>
              </td>
              <td className="hq-mono">{run.attempts}</td>
              <td className="hq-mono">{run.latency_ms ? `${run.latency_ms}ms` : '—'}</td>
              <td className="hq-mono">{run.workflow_thread_id ?? 'direct'}</td>
              <td className="text-steel-500">{relativeTime(run.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

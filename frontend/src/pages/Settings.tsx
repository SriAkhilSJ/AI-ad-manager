import { useApi } from '../hooks/useApi'
import { agentsApi, analyticsApi, systemApi } from '../lib/api'
import { useAuth } from '../hooks/useAuth'
import { formatNumber } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Card,
  CardHeader,
  MetricCard,
  Mono,
  Skeleton,
} from '../components/ui'
import { AgentChip, IntegrationRow, StatRow } from '../components/shared'

export function SettingsPage() {
  const { user } = useAuth()
  const status = useApi(() => systemApi.status())
  const agents = useApi(() => agentsApi.list())
  const usage = useApi(() => analyticsApi.usage(30))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Operations"
        title="Settings"
        description="Runtime status, integration availability and agent contracts. Configuration is environment-based; secrets are never editable or readable from this UI."
      />

      <Banner tone="neutral" title="Configuration lives in the environment">
        Every secret is read from environment variables at boot. This dashboard shows capability and
        availability — never values. To change configuration, update the backend environment and
        restart the API.
      </Banner>

      {status.loading ? (
        <Skeleton className="h-64 w-full" />
      ) : status.error ? (
        <ErrorState message={status.error.message} onRetry={status.reload} />
      ) : status.data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricCard
              label="Environment"
              value={status.data.app.environment}
              hint={`version ${status.data.app.version}`}
            />
            <MetricCard
              label="Database"
              value={status.data.database.dialect}
              hint={`vector search: ${status.data.database.vector_search}`}
              tone="ok"
            />
            <MetricCard
              label="Task driver"
              value={status.data.tasks.driver}
              hint={status.data.tasks.broker_configured ? 'broker configured' : 'no broker'}
            />
            <MetricCard
              label="Model spend (30d)"
              value={usage.data?.has_data ? `$${usage.data.totals.cost_usd.toFixed(2)}` : '—'}
              hint={
                usage.data?.has_data
                  ? `${formatNumber(usage.data.totals.total_tokens)} tokens`
                  : 'no provider configured'
              }
              unavailable={!usage.data?.has_data}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader
                title="Integrations"
                subtitle="Each row states what is genuinely available."
              />
              <div className="px-5 py-3">
                <IntegrationRow
                  name="Database"
                  status={status.data.database.status}
                  note={`${status.data.database.dialect} · pgvector: ${status.data.database.pgvector}`}
                />
                <IntegrationRow
                  name="LLM gateway"
                  status={status.data.llm.status}
                  configured={status.data.llm.routing.configured}
                  note={
                    status.data.llm.note ??
                    `default ${status.data.llm.routing.default.provider} / ${status.data.llm.routing.default.model}`
                  }
                />
                <IntegrationRow
                  name="Background tasks"
                  status={status.data.tasks.status}
                  note={`${status.data.tasks.driver} · requested: ${status.data.tasks.requested}`}
                />
                <IntegrationRow
                  name="Object storage"
                  status={status.data.storage.status}
                  note={status.data.storage.note ?? `backend: ${status.data.storage.backend}`}
                />
                <IntegrationRow
                  name="Social publishing"
                  status={status.data.publishing.status}
                  note={status.data.publishing.note}
                />
                <IntegrationRow
                  name="Token vault"
                  status={
                    status.data.publishing.status === 'available' ? 'available' : 'unavailable'
                  }
                  note="Tokens are never stored in application tables."
                />
              </div>
            </Card>

            <Card>
              <CardHeader title="Platform facts" subtitle="Read-only runtime truth." />
              <div className="px-5 py-3">
                <StatRow label="App" value={status.data.app.name} />
                <StatRow label="API prefix" value={<Mono>{status.data.app.api_prefix}</Mono>} />
                <StatRow
                  label="Uptime"
                  value={`${Math.round(status.data.app.uptime_seconds)}s`}
                />
                <StatRow label="Auth" value={status.data.auth.scheme} />
                <StatRow label="Approver role" value={status.data.auth.approver_role} />
                <StatRow label="Roles" value={status.data.auth.roles.join(', ')} />
                <StatRow label="Orchestrator" value={status.data.director.orchestrator} />
                <StatRow label="Checkpointing" value={status.data.director.checkpointing} />
                <StatRow label="Graph nodes" value={status.data.director.nodes} />
                <StatRow
                  label="Rate limit"
                  value={`${status.data.rate_limit.per_minute}/min · ${status.data.rate_limit.driver}`}
                />
                <StatRow label="Signed in as" value={`${user?.email} (${user?.role})`} />
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader
              title="Agent contracts"
              subtitle="Execution limits are enforced by the runtime, not advisory."
            />
            {agents.loading ? (
              <div className="space-y-2 p-4">
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
              </div>
            ) : agents.error ? (
              <div className="p-4">
                <ErrorState message={agents.error.message} onRetry={agents.reload} />
              </div>
            ) : (
              <table className="hq-table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Responsibilities</th>
                    <th>Attempts</th>
                    <th>Timeout</th>
                    <th>Token budget</th>
                  </tr>
                </thead>
                <tbody>
                  {(agents.data ?? []).map((agent) => (
                    <tr key={agent.name}>
                      <td>
                        <AgentChip agent={agent.name} />
                      </td>
                      <td className="max-w-md">
                        <p className="line-clamp-2 text-[11px] leading-relaxed text-steel-500">
                          {agent.responsibilities.join(' · ')}
                        </p>
                      </td>
                      <td className="hq-mono">{agent.limits.max_attempts}</td>
                      <td className="hq-mono">{agent.limits.timeout_seconds}s</td>
                      <td className="hq-mono">{agent.limits.max_tokens}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card>
            <CardHeader title="Security posture" />
            <div className="grid gap-4 px-5 py-4 sm:grid-cols-2">
              <div className="space-y-2">
                <p className="hq-label">Enforced</p>
                <ul className="space-y-1 text-[11px] text-steel-400">
                  <li>· JWT bearer auth with role claims; stale roles force re-auth.</li>
                  <li>· Founder-only approval, enforced in the service layer.</li>
                  <li>· Structured error envelope; no stack traces or vendor errors.</li>
                  <li>· Secrets redacted from every log line before emission.</li>
                </ul>
              </div>
              <div className="space-y-2">
                <p className="hq-label">Data handling</p>
                <ul className="space-y-1 text-[11px] text-steel-400">
                  <li>· Social tables hold a vault reference, never a token.</li>
                  <li>· Response schemas are allow-lists.</li>
                  <li>· Idempotency keys are scoped per user and hashed by body.</li>
                  <li>· Audit rows are written in the same transaction as mutations.</li>
                </ul>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.06] px-5 py-3">
              <Badge tone="violet">human approval required</Badge>
              <Badge tone="muted">phase 1</Badge>
              <Badge tone="muted">
                {status.data.app.bootstrap_required ? 'bootstrap pending' : 'bootstrap complete'}
              </Badge>
            </div>
          </Card>
        </>
      ) : null}
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, strategyApi, strategyDirectorApi } from '../lib/api'
import { formatDateTime, titleCase } from '../lib/format'
import { PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Input,
  KeyValue,
  Mono,
  SectionTitle,
  SkeletonRows,
  Textarea,
} from '../components/ui'
import type { StrategyWorkflowState } from '../lib/types'

const OBJECTIVE_LABEL: Record<string, string> = {
  brand_awareness: 'Brand awareness',
  developer_acquisition: 'Developer acquisition',
  waitlist_registrations: 'Waitlist registrations',
  product_adoption: 'Product adoption',
  community_growth: 'Community growth',
}

const PHASE_ORDER = [
  'intake',
  'retrieving',
  'researching',
  'evaluating',
  'strategising',
  'checking',
  'persisting',
  'awaiting_approval',
  'approved',
  'completed',
]

export function StrategyDirectorPage() {
  const { canEdit, canApprove } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const capabilities = useApi(() => strategyApi.capabilities())

  const [objective, setObjective] = useState('')
  const [objectives, setObjectives] = useState<string[]>(['developer_acquisition'])
  const [budget, setBudget] = useState('')
  const [workflowId, setWorkflowId] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  const workflows = useApi(() => strategyDirectorApi.workflows(20))

  const estimate = useApi(
    () => strategyDirectorApi.estimate(budget ? Number(budget) : undefined),
    [budget],
  )

  const state = useApi(
    () => strategyDirectorApi.status(workflowId!),
    [workflowId],
    { enabled: Boolean(workflowId) },
  )

  const start = useMutation(() =>
    strategyDirectorApi.start({
      brand_profile_id: brand.data!.id,
      objective,
      business_objectives: objectives,
      budget_usd: budget ? Number(budget) : null,
    }),
  )

  const decide = useMutation((args: { action: string; reason: string }) =>
    strategyDirectorApi.decide(workflowId!, args),
  )

  useEffect(() => {
    if (!start.error) return
    notify(start.error.message, 'danger')
  }, [start.error, notify])

  const run = async () => {
    if (!objective.trim()) {
      notify('Describe the objective first.', 'danger')
      return
    }
    const result = await start.run(undefined as never)
    if (!result) return
    setWorkflowId(result.workflow_id)
    workflows.reload()
    notify(
      result.state.status === 'awaiting_approval'
        ? 'Strategy drafted. Founder approval required.'
        : `Workflow finished with status: ${result.state.status}.`,
      result.state.status === 'awaiting_approval' ? 'ok' : 'danger',
    )
    setObjective('')
  }

  const onDecide = async (action: string) => {
    const result = await decide.run({ action, reason })
    if (!result) {
      notify(decide.error?.message ?? 'Decision failed.', 'danger')
      return
    }
    notify(`Decision recorded: ${titleCase(action)}.`, 'ok')
    setReason('')
    state.reload()
    workflows.reload()
  }

  const current: StrategyWorkflowState | null = state.data
  const awaiting = current?.awaiting_human ?? false

  const timeline = useMemo(() => {
    if (!current) return []
    const trace = (current.trace ?? []) as Array<Record<string, unknown>>
    return trace.map((entry, index) => ({
      key: `${index}-${String(entry.entry ?? '')}`,
      entry: String(entry.entry ?? 'event'),
      at: String(entry.at ?? ''),
      detail: entry,
    }))
  }, [current])

  return (
    <>
      <PageHeader
        title="Marketing Director"
        description="Research → evaluate evidence → generate strategy → brand check → founder approval. Bounded at every step."
      />

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <div className="space-y-4">
          {canEdit && (
            <Card>
              <CardHeader
                title="New objective"
                subtitle="The Director retrieves your brand knowledge, decides whether research is needed, and delegates to the research and strategy agents."
              />
              <div className="space-y-3">
                <Textarea
                  rows={2}
                  placeholder="e.g. Acquire software developers for Crusor AI this quarter"
                  value={objective}
                  onChange={(event) => setObjective(event.target.value)}
                />
                <div className="flex flex-wrap gap-1.5">
                  {(capabilities.data?.business_objectives ?? []).map((value) => {
                    const active = objectives.includes(value)
                    return (
                      <button
                        key={value}
                        type="button"
                        onClick={() =>
                          setObjectives((items) =>
                            active ? items.filter((item) => item !== value) : [...items, value],
                          )
                        }
                        className={`rounded-md border px-2.5 py-1 text-[11px] transition ${
                          active
                            ? 'border-violet-500/40 bg-violet-500/10 text-violet-200'
                            : 'border-white/[0.08] text-steel-500 hover:border-white/20 hover:text-steel-300'
                        }`}
                      >
                        {OBJECTIVE_LABEL[value] ?? titleCase(value)}
                      </button>
                    )
                  })}
                </div>

                <div className="flex flex-wrap items-center gap-3">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-steel-600">Budget $</span>
                    <Input
                      className="w-24"
                      type="number"
                      min={0}
                      step={0.01}
                      placeholder={String(capabilities.data?.budget_usd ?? 0.06)}
                      value={budget}
                      onChange={(event) => setBudget(event.target.value)}
                    />
                  </div>
                  <Button onClick={run} loading={start.pending} disabled={objectives.length === 0}>
                    Run Director
                  </Button>
                </div>

                {estimate.data && (
                  <div className="rounded-lg border border-white/[0.06] bg-white/[0.015] p-3">
                    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
                      <span className="text-[11px] text-steel-500">
                        Estimated before execution:{' '}
                        <span className="text-steel-300">
                          ${estimate.data.estimated_cost_usd.toFixed(4)} ≈ ₹
                          {estimate.data.estimated_cost_inr.toFixed(2)}
                        </span>
                      </span>
                      <span className="text-[11px] text-steel-500">
                        Budget: ${estimate.data.budget_usd.toFixed(2)} ≈ ₹
                        {estimate.data.budget_inr.toFixed(2)}
                      </span>
                      <Badge tone={estimate.data.within_budget ? 'ok' : 'warn'}>
                        {estimate.data.within_budget ? 'within budget' : 'over budget'}
                      </Badge>
                    </div>
                    <ul className="mt-2 space-y-0.5">
                      {estimate.data.assumptions.map((assumption, index) => (
                        <li key={index} className="text-[10px] leading-relaxed text-steel-600">
                          · {assumption}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </Card>
          )}

          {current && (
            <Card>
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={workflowTone(current.status)}>{titleCase(current.status)}</Badge>
                <span className="text-[12px] text-steel-300">
                  {current.phase_label ?? titleCase(current.phase ?? '')}
                </span>
                <Mono className="ml-auto text-[10px] text-steel-700">{workflowId}</Mono>
                <Button size="sm" variant="ghost" onClick={state.reload}>
                  Refresh
                </Button>
              </div>

              <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
                <KeyValue label="Iterations">
                  {current.iteration ?? 0} / {current.max_iterations ?? '—'}
                </KeyValue>
                <KeyValue label="Research rounds">
                  {current.research_rounds?.length ?? 0}
                </KeyValue>
                <KeyValue label="Evidence">
                  {current.evidence_verdict ? titleCase(current.evidence_verdict) : '—'}
                </KeyValue>
                <KeyValue label="Spend">
                  ${(current.cost_actual?.total_usd ?? 0).toFixed(4)}
                </KeyValue>
                <KeyValue label="Findings">
                  {String(
                    (current.evidence_assessment?.total_findings as number | undefined) ?? '—',
                  )}
                </KeyValue>
                <KeyValue label="Verified">
                  {String(
                    (current.evidence_assessment?.verified_findings as number | undefined) ?? '—',
                  )}
                </KeyValue>
                <KeyValue label="Strategy version">
                  {current.strategy_version_number || '—'}
                </KeyValue>
                <KeyValue label="Checkpoints">{current.checkpoint_count ?? '—'}</KeyValue>
              </div>

              {current.summary && (
                <p className="mt-3 rounded-lg border border-white/[0.06] bg-white/[0.015] p-3 text-[12px] leading-relaxed text-steel-400">
                  {current.summary}
                </p>
              )}

              {awaiting && current.approval_request && (
                <div className="mt-4 rounded-lg border border-violet-500/30 bg-violet-500/[0.06] p-3">
                  <p className="text-[12px] font-medium text-steel-200">Founder approval required</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-steel-500">
                    {String(current.approval_request.note ?? '')}
                  </p>
                  {canApprove ? (
                    <div className="mt-3 space-y-2">
                      <Input
                        placeholder="Reason (recorded in the audit trail)"
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                      />
                      <div className="flex gap-2">
                        <Button size="sm" onClick={() => onDecide('approve')} loading={decide.pending}>
                          Approve
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => onDecide('request_changes')}>
                          Request changes
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => onDecide('reject')}>
                          Reject
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <p className="mt-2 text-[11px] text-amber-400/85">
                      Only the founder can approve. The workflow is suspended until they decide.
                    </p>
                  )}
                </div>
              )}

              {current.strategy_id && (
                <div className="mt-3">
                  <a
                    href={`/strategy?selected=${current.strategy_id}`}
                    className="text-[11px] text-violet-300 hover:text-violet-200"
                  >
                    Open the generated strategy in Strategy Studio →
                  </a>
                </div>
              )}

              {(current.errors?.length ?? 0) > 0 && (
                <div className="mt-3 space-y-1">
                  {current.errors!.map((error, index) => (
                    <p key={index} className="text-[11px] text-red-300">
                      {String(error.code ?? 'danger')}: {String(error.message ?? '')}
                    </p>
                  ))}
                </div>
              )}
            </Card>
          )}

          {current && (
            <Card>
              <CardHeader
                title="Agent activity"
                subtitle="Every step the Director took, in order."
              />
              {timeline.length === 0 && <p className="text-[11px] text-steel-600">No activity yet.</p>}
              <ol className="space-y-2">
                {timeline.map((item) => (
                  <li key={item.key} className="flex gap-3">
                    <div className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-violet-400/70" />
                    <div className="min-w-0">
                      <p className="text-[12px] text-steel-300">{titleCase(item.entry)}</p>
                      <p className="text-[10px] text-steel-600">{describeEntry(item.detail)}</p>
                      {item.at && (
                        <p className="text-[9px] text-steel-700">{formatDateTime(item.at)}</p>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </Card>
          )}

          {current?.research_rounds && current.research_rounds.length > 0 && (
            <Card>
              <CardHeader title="Research rounds" subtitle="What was asked for, and what came back." />
              <div className="space-y-2">
                {current.research_rounds.map((round) => (
                  <div key={round.round} className="rounded-lg border border-white/[0.06] p-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="neutral">Round {round.round}</Badge>
                      <span className="text-[11px] text-steel-500">{round.provider}</span>
                      <span className="ml-auto text-[11px] text-steel-400">
                        {round.findings_created} finding(s)
                      </span>
                    </div>
                    {round.gaps.length > 0 && (
                      <ul className="mt-1.5 space-y-0.5">
                        {round.gaps.map((gap, index) => (
                          <li key={index} className="text-[10px] text-amber-400/80">
                            · {gap}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}

          {!current && !workflowId && (
            <Card>
              <EmptyState
                title="No workflow selected"
                description="Start a run above, or pick a recent workflow from the right."
              />
            </Card>
          )}
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader title="Workflow phases" subtitle="What the Director does, in order." />
            <ol className="space-y-1.5">
              {PHASE_ORDER.map((phase, index) => {
                const active = current?.phase === phase
                const doneIndex = PHASE_ORDER.indexOf(current?.phase ?? '')
                const done = doneIndex >= 0 && index < doneIndex
                return (
                  <li key={phase} className="flex items-center gap-2">
                    <span
                      className={`flex h-4 w-4 items-center justify-center rounded-full border text-[8px] ${
                        active
                          ? 'border-violet-400 bg-violet-500/20 text-violet-200'
                          : done
                            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                            : 'border-white/[0.1] text-steel-700'
                      }`}
                    >
                      {index + 1}
                    </span>
                    <span
                      className={`text-[11px] ${active ? 'text-steel-200' : 'text-steel-600'}`}
                    >
                      {titleCase(phase)}
                    </span>
                  </li>
                )
              })}
            </ol>
            {capabilities.data && (
              <p className="mt-3 border-t border-white/[0.05] pt-2 text-[10px] leading-relaxed text-steel-600">
                {capabilities.data.note}
              </p>
            )}
          </Card>

          <Card>
            <CardHeader title="Recent workflows" />
            {workflows.loading && <SkeletonRows rows={3} height="h-10" />}
            {workflows.data && workflows.data.length === 0 && (
              <p className="text-[11px] text-steel-600">No workflows yet.</p>
            )}
            <div className="space-y-1">
              {workflows.data?.map((workflow) => (
                <button
                  key={workflow.workflow_id}
                  type="button"
                  onClick={() => setWorkflowId(workflow.workflow_id)}
                  className={`w-full rounded border px-2 py-1.5 text-left transition ${
                    workflowId === workflow.workflow_id
                      ? 'border-violet-500/40 bg-violet-500/[0.07]'
                      : 'border-white/[0.06] hover:border-white/[0.14]'
                  }`}
                >
                  <p className="truncate text-[10px] text-steel-400">
                    {workflow.workflow_id}
                  </p>
                  <p className="text-[9px] text-steel-700">
                    {workflow.status} · step {workflow.step ?? '—'}
                  </p>
                </button>
              ))}
            </div>
          </Card>

          {current?.unverified_brand_claims && current.unverified_brand_claims.length > 0 && (
            <Card>
              <CardHeader
                title="Unconfirmed claims"
                subtitle="Excluded from the strategy until you confirm them."
              />
              <ul className="space-y-1">
                {current.unverified_brand_claims.slice(0, 8).map((claim, index) => (
                  <li key={index} className="text-[10px] leading-relaxed text-amber-400/80">
                    · {claim}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>

      <div className="mt-6" /><SectionTitle>Safety</SectionTitle>
      <Card>
        <Banner tone="info" title="What approval does and does not do">
          Approving a strategy authorises planning. It does not publish content, send email, spend
          ad budget or change a live social account — those remain unavailable in this deployment.
          Every decision is written to an append-only ledger bound to the exact strategy version,
          and any edit voids it.
        </Banner>
      </Card>
    </>
  )
}

function workflowTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  if (status === 'approved' || status === 'completed') return 'ok'
  if (status === 'awaiting_approval' || status === 'partial' || status === 'budget_exhausted')
    return 'warn'
  if (status === 'failed' || status === 'cancelled' || status === 'rejected') return 'danger'
  return 'neutral'
}

function describeEntry(entry: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(entry)) {
    if (key === 'at' || key === 'entry') continue
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      parts.push(`${key}: ${value.length}`)
      continue
    }
    if (typeof value === 'object') continue
    parts.push(`${key}: ${String(value)}`)
  }
  return parts.join(' · ')
}

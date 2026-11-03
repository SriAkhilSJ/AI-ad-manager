import { useEffect, useState } from 'react'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, researchApi, strategyApi } from '../lib/api'
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
  Select,
  SkeletonRows,
  Tabs,
  Textarea,
} from '../components/ui'

const OBJECTIVE_LABEL: Record<string, string> = {
  brand_awareness: 'Brand awareness',
  developer_acquisition: 'Developer acquisition',
  waitlist_registrations: 'Waitlist registrations',
  product_adoption: 'Product adoption',
  community_growth: 'Community growth',
}

type Tab = 'overview' | 'plan' | 'versions' | 'approval'

export function StrategyStudioPage() {
  const { canEdit, canApprove } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const capabilities = useApi(() => strategyApi.capabilities())

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [objective, setObjective] = useState('')
  const [objectives, setObjectives] = useState<string[]>(['developer_acquisition'])
  const [researchProjectId, setResearchProjectId] = useState('')
  const [decisionReason, setDecisionReason] = useState('')
  const [left, setLeft] = useState('1')
  const [right, setRight] = useState('2')

  const strategies = useApi(
    () => strategyApi.list({ brand_profile_id: brand.data!.id, limit: 50 }),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )

  const researchProjects = useApi(
    () => researchApi.projects({ brand_profile_id: brand.data!.id, limit: 20 }),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )

  const detail = useApi(
    () => strategyApi.get(selectedId!),
    [selectedId],
    { enabled: Boolean(selectedId) },
  )

  const diff = useApi(
    () => strategyApi.diff(selectedId!, Number(left), Number(right)),
    [selectedId, left, right],
    { enabled: Boolean(selectedId) && tab === 'versions' },
  )

  const generate = useMutation(() =>
    strategyApi.generate({
      brand_profile_id: brand.data!.id,
      objective,
      business_objectives: objectives,
      research_project_id: researchProjectId || null,
    }),
  )

  const decide = useMutation((args: { action: string; reason: string }) =>
    strategyApi.decide(selectedId!, { action: args.action, reason: args.reason }),
  )

  useEffect(() => {
    if (strategies.data && strategies.data.length > 0 && !selectedId) {
      setSelectedId(strategies.data[0].id)
    }
  }, [strategies.data, selectedId])

  useEffect(() => {
    if (!generate.error) return
    notify(generate.error.message, 'danger')
  }, [generate.error, notify])

  const onGenerate = async () => {
    if (!objective.trim()) {
      notify('Describe the objective for this strategy first.', 'danger')
      return
    }
    const result = await generate.run(undefined as never)
    if (!result) return
    notify(`Strategy v${result.current_version?.version_number ?? 1} drafted. It awaits founder approval.`, 'ok')
    strategies.reload()
    setSelectedId(result.strategy.id)
    setObjective('')
  }

  const onDecide = async (action: string) => {
    const result = await decide.run({ action, reason: decisionReason })
    if (!result) {
      notify(decide.error?.message ?? 'Decision failed.', 'danger')
      return
    }
    notify(
      action === 'approve'
        ? 'Strategy approved for planning. Nothing has been published or spent.'
        : `Decision recorded: ${titleCase(action)}.`,
      'ok',
    )
    setDecisionReason('')
    strategies.reload()
    detail.reload()
  }

  const version = detail.data?.current_version
  const strategy = detail.data?.strategy

  return (
    <>
      <PageHeader
        title="Strategy Studio"
        description="Evidence-driven marketing strategy, versioned and gated by founder approval. Nothing here publishes, sends or spends."
      />

      {canEdit && (
        <Card>
          <CardHeader
            title="Generate a strategy"
            subtitle="The agent maps the plan to your business objectives and prioritises measurable developer acquisition over reach."
          />
          <div className="space-y-3">
            <Textarea
              rows={2}
              placeholder="e.g. Acquire software developers for Crusor AI over the next quarter"
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
                      setObjectives((current) =>
                        active ? current.filter((o) => o !== value) : [...current, value],
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
              <Select
                className="w-72"
                value={researchProjectId}
                onChange={(event) => setResearchProjectId(event.target.value)}
              >
                <option value="">All research for this brand</option>
                {researchProjects.data?.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.finding_count} findings — {project.objective.slice(0, 50)}
                  </option>
                ))}
              </Select>
              <p className="text-[11px] text-steel-600">
                Budget ${capabilities.data?.budget_usd.toFixed(2) ?? '—'} ≈ ₹
                {capabilities.data?.budget_inr_equivalent.toFixed(2) ?? '—'} per run
              </p>
              <Button className="ml-auto" onClick={onGenerate} loading={generate.pending}>
                Generate strategy
              </Button>
            </div>
            {objectives.length === 0 && (
              <p className="text-[11px] text-amber-400/80">
                A strategy must map to at least one business objective.
              </p>
            )}
          </div>
        </Card>
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-[300px_1fr]">
        <Card>
          <CardHeader title="Strategies" subtitle={`${strategies.data?.length ?? 0} total`} />
          {strategies.loading && <SkeletonRows rows={4} height="h-14" />}
          {strategies.data && strategies.data.length === 0 && (
            <EmptyState title="No strategies" description="Generate one to begin." />
          )}
          <div className="space-y-1.5">
            {strategies.data?.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => { setSelectedId(item.id); setTab('overview') }}
                className={`w-full rounded-lg border px-3 py-2 text-left transition ${
                  selectedId === item.id
                    ? 'border-violet-500/40 bg-violet-500/[0.07]'
                    : 'border-white/[0.06] hover:border-white/[0.14]'
                }`}
              >
                <p className="truncate text-[12px] text-steel-300">{item.name}</p>
                <div className="mt-1 flex items-center gap-2">
                  <Badge tone={statusTone(item.status)}>{titleCase(item.status)}</Badge>
                  <span className="text-[10px] text-steel-600">v{item.latest_version_number}</span>
                  {item.has_valid_approval ? (
                    <Badge tone="ok">Approved</Badge>
                  ) : (
                    <span className="text-[10px] text-amber-400/70">unapproved</span>
                  )}
                </div>
              </button>
            ))}
          </div>
        </Card>

        <div>
          {!selectedId && (
            <Card>
              <EmptyState
                title="No strategy selected"
                description="Generate a strategy, or pick one from the list."
              />
            </Card>
          )}

          {detail.loading && <Card><SkeletonRows rows={6} /></Card>}
          {detail.error && <Banner tone="danger">{detail.error.message}</Banner>}

          {detail.data && strategy && (
            <>
              <Card>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-[15px] font-medium text-steel-200">{strategy.name}</h2>
                    <p className="mt-1 max-w-2xl text-[12px] leading-relaxed text-steel-500">
                      {strategy.objective}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={statusTone(strategy.status)}>{titleCase(strategy.status)}</Badge>
                    <Badge tone="neutral">v{version?.version_number ?? 0}</Badge>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
                  <KeyValue label="Business objectives">
                    {(version?.business_objectives ?? []).length}
                  </KeyValue>
                  <KeyValue label="KPIs">{(version?.kpis ?? []).length}</KeyValue>
                  <KeyValue label="Experiments">{(version?.growth_experiments ?? []).length}</KeyValue>
                  <KeyValue label="Confidence">
                    {version ? `${Math.round(version.confidence * 100)}%` : '—'}
                  </KeyValue>
                </div>

                <div
                  className={`mt-4 rounded-lg border p-3 ${
                    detail.data.can_execute
                      ? 'border-emerald-500/25 bg-emerald-500/[0.06]'
                      : 'border-amber-500/25 bg-amber-500/[0.05]'
                  }`}
                >
                  <p className="text-[12px] text-steel-300">
                    {detail.data.can_execute
                      ? 'This exact version is approved. Approval covers planning only — publishing, email, ad spend and live account changes remain unavailable.'
                      : 'Execution is blocked.'}
                  </p>
                  {!detail.data.can_execute && (
                    <p className="mt-1 text-[11px] text-steel-500">
                      {detail.data.execution_blocked_reason}
                    </p>
                  )}
                </div>
              </Card>

              <div className="mt-4">
                <Tabs
                  value={tab}
                  onChange={setTab}
                  tabs={[
                    { id: 'overview', label: 'Overview' },
                    { id: 'plan', label: 'Full plan' },
                    { id: 'versions', label: 'Versions' },
                    { id: 'approval', label: 'Approval' },
                  ]}
                />
              </div>

              {tab === 'overview' && version && (
                <div className="mt-4 space-y-4">
                  <Card>
                    <CardHeader title="Positioning" subtitle="Evidence-backed recommendations only." />
                    <RecordList rows={version.positioning_recommendations} primary="statement" secondary="rationale" />
                  </Card>

                  <Card>
                    <CardHeader
                      title="Business objective alignment"
                      subtitle="Every objective needs at least one pillar, experiment or KPI serving it."
                    />
                    <div className="space-y-2">
                      {Object.entries(
                        (version.objective_alignment?.map as Record<string, string[]>) ?? {},
                      ).map(([objective, items]) => (
                        <div key={objective} className="rounded-lg border border-white/[0.06] p-2.5">
                          <div className="flex items-center gap-2">
                            <Badge tone={items.length ? 'ok' : 'warn'}>
                              {OBJECTIVE_LABEL[objective] ?? titleCase(objective)}
                            </Badge>
                            <span className="text-[10px] text-steel-600">
                              {items.length} supporting item(s)
                            </span>
                          </div>
                          {items.length > 0 && (
                            <p className="mt-1.5 text-[11px] text-steel-500">{items.join(' · ')}</p>
                          )}
                        </div>
                      ))}
                    </div>
                    {(version.objective_alignment?.advisory_only as boolean) && (
                      <Banner tone="warn" title="Reach-only KPIs">
                        This strategy measures attention rather than acquisition. Add a signup,
                        waitlist or activation metric before relying on it.
                      </Banner>
                    )}
                  </Card>

                  <Card>
                    <CardHeader
                      title="Audience segments"
                      subtitle="Founder-stated segments are assumptions until research confirms them."
                    />
                    <RecordList rows={version.audience_segmentation} primary="segment" secondary="why" />
                  </Card>

                  <Card>
                    <CardHeader title="Differentiation" />
                    <RecordList
                      rows={version.competitor_differentiation}
                      primary="competitor"
                      secondary="differentiation"
                    />
                  </Card>
                </div>
              )}

              {tab === 'plan' && version && (
                <div className="mt-4 space-y-4">
                  <Card>
                    <CardHeader title="KPIs" subtitle="Acquisition metrics take precedence over reach." />
                    <div className="space-y-1.5">
                      {version.kpis.map((kpi, index) => (
                        <div
                          key={index}
                          className="flex flex-wrap items-center gap-2 rounded-lg border border-white/[0.06] px-3 py-2"
                        >
                          <Mono>{String(kpi.name ?? '—')}</Mono>
                          {kpi.is_vanity ? (
                            <Badge tone="warn">vanity</Badge>
                          ) : (
                            <Badge tone="ok">acquisition</Badge>
                          )}
                          <span className="text-[11px] text-steel-600">
                            {String(kpi.definition ?? '') as string}
                          </span>
                          <span className="ml-auto text-[10px] text-steel-700">
                            {String(kpi.source ?? '')}
                          </span>
                        </div>
                      ))}
                    </div>
                  </Card>

                  <Card>
                    <CardHeader
                      title="Measurement plan"
                      subtitle="Definitions and required instrumentation — never invented numbers."
                    />
                    {Boolean(version.measurement_plan?.disclaimer) && (
                      <Banner tone="warn">{String(version.measurement_plan.disclaimer)}</Banner>
                    )}
                    <div className="mt-3 space-y-2">
                      <KeyValue label="Targets">
                        {String(version.measurement_plan?.targets ?? 'None set')}
                      </KeyValue>
                      <KeyValue label="Review cadence">
                        {String(version.measurement_plan?.review_cadence ?? '—')}
                      </KeyValue>
                      <div>
                        <p className="text-[10px] uppercase tracking-brand text-steel-700">
                          Instrumentation required
                        </p>
                        <ul className="mt-1 space-y-1">
                          {((version.measurement_plan?.instrumentation_required as string[]) ?? []).map(
                            (item, index) => (
                              <li key={index} className="text-[11px] text-steel-500">
                                · {item}
                              </li>
                            ),
                          )}
                        </ul>
                      </div>
                    </div>
                  </Card>

                  <Card>
                    <CardHeader title="Content pillars" />
                    <RecordList rows={version.content_pillars} primary="name" secondary="purpose" />
                  </Card>

                  <Card>
                    <CardHeader title="Campaign concepts" />
                    <RecordList rows={version.campaign_concepts} primary="name" secondary="concept" />
                  </Card>

                  <Card>
                    <CardHeader title="Platform strategy" />
                    <div className="space-y-1.5">
                      {version.platform_strategies.map((row, index) => (
                        <div
                          key={index}
                          className="flex flex-wrap items-center gap-3 rounded-lg border border-white/[0.06] px-3 py-2"
                        >
                          <Badge tone="neutral">{String(row.platform ?? '—')}</Badge>
                          <span className="text-[11px] text-steel-500">{String(row.role ?? '')}</span>
                          <span className="ml-auto text-[10px] text-steel-600">
                            {String(row.frequency ?? '')}
                          </span>
                        </div>
                      ))}
                    </div>
                  </Card>

                  <Card>
                    <CardHeader title="Weekly objectives" />
                    <div className="space-y-1.5">
                      {version.weekly_objectives.map((row, index) => (
                        <div key={index} className="rounded-lg border border-white/[0.06] px-3 py-2">
                          <div className="flex items-center gap-2">
                            <Badge tone="neutral">Week {String(row.week ?? index + 1)}</Badge>
                            <span className="text-[12px] text-steel-300">{String(row.focus ?? '')}</span>
                          </div>
                          <p className="mt-1 text-[11px] text-steel-600">{String(row.deliverable ?? '')}</p>
                        </div>
                      ))}
                    </div>
                  </Card>

                  <Card>
                    <CardHeader title="Lead generation" />
                    <RecordList rows={version.lead_generation} primary="mechanism" secondary="detail" />
                  </Card>

                  <Card>
                    <CardHeader
                      title="Growth experiments"
                      subtitle={
                        detail.data.experiments.length
                          ? 'Tracked experiments, created on approval.'
                          : 'Proposed experiments. They become tracked records once approved.'
                      }
                    />
                    <RecordList rows={version.growth_experiments} primary="name" secondary="hypothesis" />
                  </Card>

                  {(version.warnings.length > 0 || version.missing_information.length > 0) && (
                    <Card>
                      <CardHeader title="Open questions and warnings" />
                      <ul className="space-y-1.5">
                        {version.warnings.map((warning, index) => (
                          <li key={`w-${index}`} className="text-[11px] text-amber-400/85">
                            · {warning}
                          </li>
                        ))}
                        {version.missing_information.map((item, index) => (
                          <li key={`m-${index}`} className="text-[11px] text-steel-500">
                            · <span className="text-steel-300">{String(item.area)}:</span>{' '}
                            {String(item.detail)}
                            {item.blocks_decision ? (
                              <span className="ml-1 text-red-300">(blocks decision)</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    </Card>
                  )}
                </div>
              )}

              {tab === 'versions' && (
                <div className="mt-4 space-y-4">
                  <Card>
                    <CardHeader
                      title="Version history"
                      subtitle="Versions are immutable. Editing creates a new one and invalidates any approval."
                    />
                    <div className="space-y-1.5">
                      {detail.data.versions.map((row) => (
                        <div
                          key={row.id}
                          className="flex flex-wrap items-center gap-3 rounded-lg border border-white/[0.06] px-3 py-2"
                        >
                          <Badge tone="neutral">v{row.version_number}</Badge>
                          <span className="text-[11px] text-steel-500">{row.change_note}</span>
                          <Badge tone="neutral">{titleCase(row.generation_mode)}</Badge>
                          <span className="text-[10px] text-steel-700">
                            {row.created_at ? formatDateTime(row.created_at) : ''}
                          </span>
                          <Mono className="ml-auto text-[9px] text-steel-700">
                            {row.content_hash.slice(0, 12)}
                          </Mono>
                        </div>
                      ))}
                    </div>
                  </Card>

                  <Card>
                    <CardHeader title="Compare versions" />
                    <div className="flex items-end gap-3">
                      <Input
                        className="w-20"
                        type="number"
                        min={1}
                        value={left}
                        onChange={(event) => setLeft(event.target.value)}
                      />
                      <span className="pb-2 text-[11px] text-steel-600">vs</span>
                      <Input
                        className="w-20"
                        type="number"
                        min={1}
                        value={right}
                        onChange={(event) => setRight(event.target.value)}
                      />
                    </div>
                    <div className="mt-3">
                      {diff.error && <Banner tone="warn">{diff.error.message}</Banner>}
                      {diff.data && diff.data.identical && (
                        <p className="text-[11px] text-steel-600">
                          These versions have identical strategy content.
                        </p>
                      )}
                      {diff.data && !diff.data.identical && (
                        <div className="space-y-1">
                          <p className="text-[11px] text-steel-500">
                            {diff.data.changed_fields.length} field(s) changed:
                          </p>
                          <div className="flex flex-wrap gap-1.5">
                            {diff.data.changed_fields.map((field) => (
                              <Badge key={field} tone="neutral">
                                {titleCase(field)}
                              </Badge>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </Card>
                </div>
              )}

              {tab === 'approval' && (
                <div className="mt-4 space-y-4">
                  <Card>
                    <CardHeader
                      title="Founder decision"
                      subtitle="Approval binds to this exact version and its content hash. Any edit voids it."
                    />
                    {canApprove ? (
                      <div className="space-y-3">
                        <Textarea
                          rows={2}
                          placeholder="Reason for the decision (recorded in the audit trail)"
                          value={decisionReason}
                          onChange={(event) => setDecisionReason(event.target.value)}
                        />
                        <div className="flex flex-wrap gap-2">
                          <Button
                            onClick={() => onDecide('approve')}
                            loading={decide.pending}
                            disabled={strategy.status === 'APPROVED'}
                          >
                            Approve v{version?.version_number}
                          </Button>
                          <Button variant="ghost" onClick={() => onDecide('request_changes')}>
                            Request changes
                          </Button>
                          <Button variant="danger" onClick={() => onDecide('reject')}>
                            Reject
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <Banner tone="warn">
                        Only the founder can approve or reject a strategy. You can review everything
                        here, but the decision is theirs.
                      </Banner>
                    )}
                  </Card>

                  <Card>
                    <CardHeader title="Approval history" subtitle="Append-only ledger." />
                    {detail.data.approval_history.length === 0 && (
                      <p className="text-[11px] text-steel-600">No decisions recorded yet.</p>
                    )}
                    <div className="space-y-1.5">
                      {detail.data.approval_history.map((row) => (
                        <div
                          key={row.id}
                          className="flex flex-wrap items-center gap-2 rounded-lg border border-white/[0.06] px-3 py-2"
                        >
                          <Badge
                            tone={
                              row.action === 'approve' ? (row.is_valid ? 'ok' : 'neutral') : 'warn'
                            }
                          >
                            {titleCase(row.action)}
                          </Badge>
                          <span className="text-[11px] text-steel-500">{row.reason || '—'}</span>
                          <span className="ml-auto text-[10px] text-steel-700">
                            {row.created_at ? formatDateTime(row.created_at) : ''}
                          </span>
                          {!row.is_valid && (
                            <span className="w-full text-[10px] text-amber-400/80">
                              invalidated: {row.invalidated_reason || 'superseded'}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </Card>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {capabilities.data && (
        <>
          <div className="mt-6" /><SectionTitle>Model routing and budget</SectionTitle>
          <Card>
            <div className="grid gap-4 sm:grid-cols-3">
              <KeyValue label="Reasoning model">
                <Mono>{capabilities.data.models.reasoning ?? 'not configured'}</Mono>
              </KeyValue>
              <KeyValue label="Auxiliary model">
                <Mono>{capabilities.data.models.auxiliary ?? 'not configured'}</Mono>
              </KeyValue>
              <KeyValue label="Budget per run">
                ${capabilities.data.budget_usd.toFixed(2)} ≈ ₹
                {capabilities.data.budget_inr_equivalent.toFixed(2)}
              </KeyValue>
            </div>
            <p className="mt-3 text-[11px] leading-relaxed text-steel-600">
              {capabilities.data.note} A ₹5-equivalent envelope is not assumed to cover a complex
              research task; the run stops when the budget is reached and reports what it did not do.
            </p>
          </Card>
        </>
      )}
    </>
  )
}

function RecordList({
  rows,
  primary,
  secondary,
}: {
  rows: Array<Record<string, unknown>>
  primary: string
  secondary: string
}) {
  if (!rows.length) {
    return <p className="text-[11px] text-steel-600">Nothing recorded in this section.</p>
  }
  return (
    <div className="space-y-1.5">
      {rows.map((row, index) => (
        <div key={index} className="rounded-lg border border-white/[0.06] px-3 py-2">
          <p className="text-[12px] text-steel-300">{String(row[primary] ?? `Item ${index + 1}`)}</p>
          {row[secondary] ? (
            <p className="mt-0.5 text-[11px] leading-relaxed text-steel-600">
              {String(row[secondary])}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  )
}

function statusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  if (status === 'APPROVED') return 'ok'
  if (status === 'REJECTED') return 'danger'
  if (status === 'CHANGES_REQUESTED') return 'warn'
  return 'neutral'
}

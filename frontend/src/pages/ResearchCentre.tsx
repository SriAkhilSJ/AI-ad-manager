import { useEffect, useMemo, useState } from 'react'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, researchApi } from '../lib/api'
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

const CATEGORY_LABEL: Record<string, string> = {
  competitor_identification: 'Competitors',
  competitor_positioning: 'Competitor positioning',
  feature_comparison: 'Feature comparison',
  competitor_content_strategy: 'Competitor content',
  developer_pain_points: 'Pain points',
  market_trends: 'Market trends',
  developer_communities: 'Communities',
  content_opportunities: 'Opportunities',
  audience_questions_objections: 'Questions & objections',
}

type Tab = 'findings' | 'projects' | 'competitors'

export function ResearchCentrePage() {
  const { canEdit, user } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const capabilities = useApi(() => researchApi.capabilities())

  const [tab, setTab] = useState<Tab>('findings')
  const [objective, setObjective] = useState('')
  const [categories, setCategories] = useState<string[]>([])
  const [categoryFilter, setCategoryFilter] = useState('')
  const [verificationFilter, setVerificationFilter] = useState('')
  const [minConfidence, setMinConfidence] = useState('')

  const projects = useApi(
    () => researchApi.projects({ brand_profile_id: brand.data!.id, limit: 50 }),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )

  const findings = useApi(
    () =>
      researchApi.findings({
        brand_profile_id: brand.data!.id,
        category: categoryFilter || undefined,
        verification_status: verificationFilter || undefined,
        min_confidence: minConfidence ? Number(minConfidence) : undefined,
        limit: 100,
      }),
    [brand.data?.id, categoryFilter, verificationFilter, minConfidence],
    { enabled: Boolean(brand.data?.id) },
  )

  const competitors = useApi(
    () => researchApi.competitors(brand.data!.id),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )

  const [verifyTarget, setVerifyTarget] = useState<string | null>(null)
  const [verifyUrl, setVerifyUrl] = useState('')

  const verify = useMutation((args: { id: string; url: string }) =>
    researchApi.verify(args.id, args.url),
  )

  const start = useMutation(() =>
    researchApi.create({
      brand_profile_id: brand.data!.id,
      objective,
      categories: categories.length ? categories : undefined,
      run_now: true,
    }),
  )

  useEffect(() => {
    if (!start.error) return
    notify(start.error.message, 'danger')
  }, [start.error, notify])

  const onStart = async () => {
    if (!objective.trim()) {
      notify('Describe what you want researched first.', 'danger')
      return
    }
    const result = await start.run(undefined as never)
    if (!result) return
    notify(
      `Collected ${result.findings_created} finding(s) from ${result.sources_created} source(s).`,
      'ok',
    )
    projects.reload()
    findings.reload()
    setObjective('')
    setCategories([])
  }

  const onVerify = async () => {
    if (!verifyTarget) return
    const result = await verify.run({ id: verifyTarget, url: verifyUrl })
    if (!result) {
      notify(verify.error?.message ?? 'Could not verify that finding.', 'danger')
      return
    }
    notify('Finding verified against the supplied source.', 'ok')
    setVerifyTarget(null)
    setVerifyUrl('')
    findings.reload()
  }

  const provider = capabilities.data?.provider
  const categoryOptions = useMemo(
    () =>
      (capabilities.data?.categories ?? []).map((value) => ({
        value,
        label: CATEGORY_LABEL[value] ?? titleCase(value),
      })),
    [capabilities.data],
  )

  return (
    <>
      <PageHeader
        title="Research Centre"
        description="Source-backed market and developer research. Every finding carries its provenance, and anything unconfirmed is labelled as such."
        actions={
          <Button variant="ghost" onClick={() => { projects.reload(); findings.reload() }}>
            Refresh
          </Button>
        }
      />

      {provider && (
        <Banner
          tone={provider.status === 'available' ? 'ok' : provider.status === 'degraded' ? 'warn' : 'danger'}
          title={
            provider.status === 'available'
              ? 'Live research provider configured'
              : provider.status === 'degraded'
                ? 'Offline research provider — findings are derived from stored brand knowledge'
                : 'Research unavailable'
          }
        >
          {provider.note}
        </Banner>
      )}

      {canEdit && (
        <Card>
          <CardHeader
            title="Start research"
            subtitle="The agent runs every requested category and records each finding with its source. Categories it cannot answer are returned as gaps, never filled with assumptions."
          />
          <div className="space-y-3">
            <Textarea
              rows={2}
              placeholder="e.g. Map direct and indirect competitors for an AI-powered IDE and identify content gaps"
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
            />
            <div className="flex flex-wrap gap-1.5">
              {categoryOptions.map((option) => {
                const active = categories.includes(option.value)
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() =>
                      setCategories((current) =>
                        active ? current.filter((c) => c !== option.value) : [...current, option.value],
                      )
                    }
                    className={`rounded-md border px-2.5 py-1 text-[11px] transition ${
                      active
                        ? 'border-violet-500/40 bg-violet-500/10 text-violet-200'
                        : 'border-white/[0.08] text-steel-500 hover:border-white/20 hover:text-steel-300'
                    }`}
                  >
                    {option.label}
                  </button>
                )
              })}
            </div>
            <div className="flex items-center justify-between gap-3">
              <p className="text-[11px] text-steel-600">
                {categories.length === 0
                  ? 'All nine categories will run.'
                  : `${categories.length} categor${categories.length === 1 ? 'y' : 'ies'} selected.`}
              </p>
              <Button onClick={onStart} loading={start.pending}>
                Run research
              </Button>
            </div>
          </div>
        </Card>
      )}

      <div className="mt-6">
        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: 'findings', label: 'Findings' },
            { id: 'projects', label: 'Research jobs' },
            { id: 'competitors', label: 'Competitors' },
          ]}
        />
      </div>

      {tab === 'findings' && (
        <Card>
          <CardHeader
            title="Findings"
            subtitle={
              findings.data
                ? `${findings.data.total} finding(s), ordered by confidence.`
                : 'Loading findings…'
            }
          />
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <Select
              value={categoryFilter}
              onChange={(event) => setCategoryFilter(event.target.value)}
            >
              <option value="">All categories</option>
              {categoryOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
            <Select
              value={verificationFilter}
              onChange={(event) => setVerificationFilter(event.target.value)}
            >
              <option value="">Any verification status</option>
              {(capabilities.data?.verification_statuses ?? []).map((status) => (
                <option key={status} value={status}>
                  {titleCase(status)}
                </option>
              ))}
            </Select>
            <Input
              type="number"
              min={0}
              max={1}
              step={0.05}
              placeholder="Min confidence (0–1)"
              value={minConfidence}
              onChange={(event) => setMinConfidence(event.target.value)}
            />
          </div>

          {findings.loading && <SkeletonRows rows={5} />}
          {findings.error && <Banner tone="danger">{findings.error.message}</Banner>}
          {findings.data && findings.data.items.length === 0 && (
            <EmptyState
              title="No findings yet"
              description="Run a research job above. With no live provider configured, findings are derived from your stored brand knowledge — and open questions are labelled as gaps."
            />
          )}

          <div className="space-y-2">
            {findings.data?.items.map((finding) => (
              <div
                key={finding.id}
                className="rounded-lg border border-white/[0.06] bg-white/[0.015] p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="neutral">
                    {CATEGORY_LABEL[finding.category] ?? titleCase(finding.category)}
                  </Badge>
                  <VerificationBadge status={finding.verification_status} />
                  <span className="text-[11px] text-steel-600">
                    confidence {Math.round(finding.confidence * 100)}%
                  </span>
                  {finding.competitor_name && (
                    <span className="text-[11px] text-steel-500">{finding.competitor_name}</span>
                  )}
                  <span className="ml-auto text-[10px] text-steel-700">
                    {formatDateTime(finding.created_at)}
                  </span>
                </div>

                <p className="mt-2 text-[13px] leading-relaxed text-steel-300">{finding.finding}</p>

                {finding.evidence_summary && (
                  <p className="mt-1 text-[11px] leading-relaxed text-steel-600">
                    {finding.evidence_summary}
                  </p>
                )}

                <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-white/[0.04] pt-2">
                  {finding.source_url ? (
                    <a
                      href={finding.source_url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="truncate text-[11px] text-violet-300 hover:text-violet-200"
                    >
                      {finding.source_title || finding.source_url}
                    </a>
                  ) : (
                    <span className="text-[11px] text-amber-400/80">
                      No source — cannot be verified
                    </span>
                  )}
                  {finding.retrieved_at && (
                    <span className="text-[10px] text-steel-700">
                      retrieved {formatDateTime(finding.retrieved_at)}
                    </span>
                  )}
                  {user?.role === 'founder' &&
                    finding.verification_status !== 'verified' &&
                    (verifyTarget === finding.id ? (
                      <div className="ml-auto flex items-center gap-2">
                        <Input
                          className="w-64"
                          placeholder="https://source…"
                          value={verifyUrl}
                          onChange={(event) => setVerifyUrl(event.target.value)}
                        />
                        <Button size="sm" onClick={onVerify} loading={verify.pending}>
                          Confirm
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setVerifyTarget(null)}>
                          Cancel
                        </Button>
                      </div>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ml-auto"
                        onClick={() => setVerifyTarget(finding.id)}
                      >
                        Verify with source
                      </Button>
                    ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {tab === 'projects' && (
        <Card>
          <CardHeader
            title="Research jobs"
            subtitle="Each job reports what it actually collected, what it cost, and why it stopped."
          />
          {projects.loading && <SkeletonRows rows={4} />}
          {projects.data && projects.data.length === 0 && (
            <EmptyState title="No research jobs" description="Start one above to see its activity here." />
          )}
          <div className="space-y-2">
            {projects.data?.map((project) => (
              <div key={project.id} className="rounded-lg border border-white/[0.06] p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill status={project.status} />
                  <span className="text-[12px] text-steel-300">{project.objective}</span>
                  <span className="ml-auto text-[10px] text-steel-700">
                    {formatDateTime(project.created_at)}
                  </span>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
                  <KeyValue label="Findings">{project.finding_count}</KeyValue>
                  <KeyValue label="Sources">{project.source_count}</KeyValue>
                  <KeyValue label="Gaps">{project.gap_count}</KeyValue>
                  <KeyValue label="Provider">
                    <Mono>{project.provider || '—'}</Mono>
                  </KeyValue>
                  <KeyValue label="Spend">
                    ${project.actual_cost_usd.toFixed(4)} / ${project.max_cost_usd.toFixed(4)}
                  </KeyValue>
                  <KeyValue label="Tokens">{project.tokens_used}</KeyValue>
                  <KeyValue label="Iterations">{project.iterations}</KeyValue>
                  <KeyValue label="Rounds / calls">
                    {project.iterations} / {project.tool_calls}
                  </KeyValue>
                </div>
                {project.summary && (
                  <p className="mt-2 border-t border-white/[0.04] pt-2 text-[11px] leading-relaxed text-steel-600">
                    {project.summary}
                  </p>
                )}
                {project.error_message && (
                  <p className="mt-1 text-[11px] text-red-300">{project.error_message}</p>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {tab === 'competitors' && (
        <Card>
          <CardHeader
            title="Competitor profiles"
            subtitle="Profiles are names until evidence backs them. The count shows how many sourced findings support each one."
          />
          {competitors.data && competitors.data.length === 0 && (
            <EmptyState
              title="No competitor profiles"
              description="Add competitors on the Brand Knowledge page, then run research to gather evidence for each."
            />
          )}
          <div className="space-y-2">
            {competitors.data?.map((competitor) => (
              <div
                key={competitor.name}
                className="flex flex-wrap items-center gap-3 rounded-lg border border-white/[0.06] p-3"
              >
                <div className="min-w-0">
                  <p className="text-[13px] text-steel-300">{competitor.name}</p>
                  {competitor.positioning ? (
                    <p className="mt-0.5 text-[11px] text-steel-600">{competitor.positioning}</p>
                  ) : (
                    <p className="mt-0.5 text-[11px] text-amber-400/80">
                      No positioning recorded — do not claim a comparison yet.
                    </p>
                  )}
                </div>
                <div className="ml-auto flex items-center gap-3">
                  <Badge tone={competitor.findings > 0 ? 'ok' : 'warn'}>
                    {competitor.findings} finding{competitor.findings === 1 ? '' : 's'}
                  </Badge>
                  {competitor.website && (
                    <a
                      href={competitor.website}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="text-[11px] text-violet-300 hover:text-violet-200"
                    >
                      site
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="mt-6" /><SectionTitle>How to read this page</SectionTitle>
      <Card>
        <ul className="space-y-1.5 text-[12px] leading-relaxed text-steel-500">
          <li>
            <span className="text-steel-300">Verified</span> means the finding cites a resolvable
            source. It never means the claim has been independently checked.
          </li>
          <li>
            <span className="text-steel-300">Needs confirmation</span> means the system recorded an
            open question instead of inventing an answer.
          </li>
          <li>
            A finding with no source can be promoted to verified only by a founder supplying a real
            URL — the API refuses anything else.
          </li>
        </ul>
      </Card>
    </>
  )
}

function VerificationBadge({ status }: { status: string }) {
  const tone =
    status === 'verified'
      ? 'ok'
      : status === 'needs_confirmation'
        ? 'warn'
        : status === 'disputed'
          ? 'danger'
          : 'neutral'
  return <Badge tone={tone}>{titleCase(status)}</Badge>
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === 'succeeded'
      ? 'ok'
      : status === 'partial'
        ? 'warn'
        : status === 'failed' || status === 'budget_exceeded'
          ? 'danger'
          : status === 'unavailable'
            ? 'danger'
            : 'neutral'
  return <Badge tone={tone}>{titleCase(status)}</Badge>
}

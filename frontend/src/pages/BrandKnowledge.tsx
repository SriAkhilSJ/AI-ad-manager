import { useEffect, useState } from 'react'
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
  Divider,
  EmptyState,
  Input,
  Mono,
  SectionTitle,
  Skeleton,
  SkeletonRows,
  Textarea,
} from '../components/ui'
import { IconBrand } from '../components/layout/icons'

export function BrandKnowledgePage() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const [draft, setDraft] = useState<Partial<Record<string, string>>>({})

  useEffect(() => {
    if (brand.data) {
      setDraft({
        tagline: brand.data.tagline,
        positioning: brand.data.positioning,
        audience: brand.data.audience.join('\n'),
        personality: brand.data.personality.join('\n'),
        tone_of_voice: brand.data.tone_of_voice.join('\n'),
        do_not_claim: brand.data.do_not_claim.join('\n'),
        prohibited_phrases: brand.data.prohibited_phrases.join('\n'),
        compliance_notes: brand.data.compliance_notes,
      })
    }
  }, [brand.data])

  const goals = useApi(() => brandApi.goals(brand.data!.id), [brand.data?.id], {
    enabled: Boolean(brand.data?.id),
  })
  const pillars = useApi(() => brandApi.pillars(brand.data!.id), [brand.data?.id], {
    enabled: Boolean(brand.data?.id),
  })
  const competitors = useApi(() => brandApi.competitors(brand.data!.id), [brand.data?.id], {
    enabled: Boolean(brand.data?.id),
  })

  // Phase 2: what the system knows, and what it still needs confirmed.
  const findings = useApi(
    () => researchApi.findings({ brand_profile_id: brand.data!.id, limit: 500 }),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )
  const evidence = useApi(
    () => researchApi.competitors(brand.data!.id),
    [brand.data?.id],
    { enabled: Boolean(brand.data?.id) },
  )

  const save = useMutation(() =>
    brandApi.update(brand.data!.id, {
      tagline: draft.tagline,
      positioning: draft.positioning,
      audience: (draft.audience ?? '').split('\n').filter(Boolean),
      personality: (draft.personality ?? '').split('\n').filter(Boolean),
      tone_of_voice: (draft.tone_of_voice ?? '').split('\n').filter(Boolean),
      do_not_claim: (draft.do_not_claim ?? '').split('\n').filter(Boolean),
      prohibited_phrases: (draft.prohibited_phrases ?? '').split('\n').filter(Boolean),
      compliance_notes: draft.compliance_notes,
    }),
  )

  const [newGoal, setNewGoal] = useState('')
  const [newPillar, setNewPillar] = useState('')
  const addGoal = useMutation(() =>
    brandApi.createGoal(brand.data!.id, { objective: newGoal, kpi: '', horizon: 'quarter' }),
  )
  const addPillar = useMutation(() =>
    brandApi.createPillar(brand.data!.id, { name: newPillar, platforms: [] }),
  )

  if (brand.loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (brand.error || !brand.data) {
    return (
      <div className="space-y-4">
        <PageHeader eyebrow="Knowledge base" title="Brand Knowledge" />
        <Card>
          <EmptyState
            icon={<IconBrand />}
            title="No brand profile"
            description={
              brand.error?.message ??
              'Seed the Crusor AI profile with `python -m app.seed.brand_crusor` or create a profile via the API.'
            }
          />
        </Card>
        <Banner tone="neutral" title="Why this matters">
          Agents read every fact from this table at run time. Nothing about the product, audience or
          claim limits is hard-coded, so marketing can be re-positioned without a deployment.
        </Banner>
      </div>
    )
  }

  const data = brand.data

  const allFindings = findings.data?.items ?? []
  const verifiedFacts = allFindings.filter(
    (finding) => finding.category === 'feature_comparison' && finding.verification_status === 'verified',
  )
  const unverifiedFacts = allFindings.filter(
    (finding) => finding.category === 'feature_comparison' && finding.verification_status !== 'verified',
  )
  const unverifiedClaims = allFindings
    .filter((finding) => finding.verification_status === 'needs_confirmation')
    .map((finding) => finding.finding)
  const audienceSegments = data.audience

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={`Profile v${data.version} · ${data.is_active ? 'active' : 'inactive'}`}
        title={`${data.company} · ${data.product}`}
        description="The single source of truth for every agent. Update it here; no code change is required."
        actions={
          canEdit ? (
            <Button
              variant="primary"
              loading={save.pending}
              onClick={async () => {
                const result = await save.run(undefined as never)
                if (result) {
                  notify('Brand profile updated (version bumped, audit written).', 'ok')
                  brand.reload()
                }
              }}
            >
              Save changes
            </Button>
          ) : undefined
        }
      />

      {save.error ? <Banner tone="danger">{save.error.message}</Banner> : null}

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title="Positioning" subtitle="Read by the Research and Strategy agents." />
          <div className="space-y-3 px-5 py-4">
            <Input
              label="Tagline"
              value={draft.tagline ?? ''}
              disabled={!canEdit}
              onChange={(event) => setDraft((d) => ({ ...d, tagline: event.target.value }))}
            />
            <Textarea
              label="Positioning statement"
              value={draft.positioning ?? ''}
              disabled={!canEdit}
              onChange={(event) => setDraft((d) => ({ ...d, positioning: event.target.value }))}
            />
            <Textarea
              label="Audience (one per line)"
              value={draft.audience ?? ''}
              disabled={!canEdit}
              className="min-h-[140px]"
              onChange={(event) => setDraft((d) => ({ ...d, audience: event.target.value }))}
            />
          </div>
        </Card>

        <Card>
          <CardHeader title="Voice & personality" subtitle="Checked mechanically by the Brand Guardian." />
          <div className="space-y-3 px-5 py-4">
            <Textarea
              label="Personality traits (one per line)"
              value={draft.personality ?? ''}
              disabled={!canEdit}
              className="min-h-[110px]"
              onChange={(event) => setDraft((d) => ({ ...d, personality: event.target.value }))}
            />
            <Textarea
              label="Tone of voice (one per line)"
              value={draft.tone_of_voice ?? ''}
              disabled={!canEdit}
              className="min-h-[110px]"
              onChange={(event) => setDraft((d) => ({ ...d, tone_of_voice: event.target.value }))}
            />
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Claim guardrails"
            subtitle="Blocking rules. Copy containing these is refused."

            action={<Badge tone="danger">{data.do_not_claim.length} guards</Badge>}
          />
          <div className="space-y-3 px-5 py-4">
            <Textarea
              label="Do not claim (one per line)"
              value={draft.do_not_claim ?? ''}
              disabled={!canEdit}
              className="min-h-[140px]"
              onChange={(event) => setDraft((d) => ({ ...d, do_not_claim: event.target.value }))}
            />
            <Textarea
              label="Prohibited phrases (one per line)"
              value={draft.prohibited_phrases ?? ''}
              disabled={!canEdit}
              className="min-h-[110px]"
              onChange={(event) =>
                setDraft((d) => ({ ...d, prohibited_phrases: event.target.value }))
              }
            />
            <Textarea
              label="Compliance notes"
              value={draft.compliance_notes ?? ''}
              disabled={!canEdit}
              className="min-h-[80px]"
              onChange={(event) => setDraft((d) => ({ ...d, compliance_notes: event.target.value }))}
            />
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Visual direction"
            subtitle="Consumed by the Creative Agent for art direction."
          />
          <div className="px-5 py-4">
            <div className="space-y-2">
              {Object.entries(data.visual_direction ?? {}).map(([key, value]) => (
                <div key={key} className="flex items-start justify-between gap-4 py-1">
                  <span className="text-[11px] uppercase tracking-brand text-steel-600">
                    {titleCase(key)}
                  </span>
                  <span className="max-w-[60%] text-right text-[11px] text-steel-300">
                    {String(value)}
                  </span>
                </div>
              ))}
            </div>
            <p className="mt-3 text-[10px] text-steel-600">
              Visual direction is edited via the API (PATCH /brand/{"{id}"}).
            </p>
            <div className="mt-3 border-t border-white/[0.05] pt-3">
              <SectionTitle>Profile metadata</SectionTitle>
              <p className="hq-mono">id {data.id}</p>
              <p className="hq-mono">updated {formatDateTime(data.updated_at)}</p>
            </div>
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card>
          <CardHeader title="Marketing goals" subtitle="Drives research focus and success metrics." />
          <div className="px-5 py-4">
            {goals.loading ? (
              <Skeleton className="h-16 w-full" />
            ) : (goals.data ?? []).length === 0 ? (
              <p className="text-[11px] text-steel-600">No goals defined.</p>
            ) : (
              <ul className="space-y-2">
                {(goals.data ?? []).map((goal) => (
                  <li key={goal.id} className="rounded-md border border-white/[0.05] px-2.5 py-2">
                    <p className="text-[11px] text-steel-300">{goal.objective}</p>
                    <p className="mt-0.5 hq-mono">
                      {goal.horizon} · priority {goal.priority}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {canEdit ? (
              <div className="mt-3 flex gap-2">
                <Input
                  value={newGoal}
                  placeholder="New objective"
                  onChange={(event) => setNewGoal(event.target.value)}
                  className="py-1.5 text-[11px]"
                />
                <Button
                  size="sm"
                  loading={addGoal.pending}
                  onClick={async () => {
                    if (!newGoal.trim()) return
                    const result = await addGoal.run(undefined as never)
                    if (result) {
                      setNewGoal('')
                      goals.reload()
                    }
                  }}
                >
                  Add
                </Button>
              </div>
            ) : null}
          </div>
        </Card>

        <Card>
          <CardHeader title="Content pillars" subtitle="Weights steer the strategy mix." />
          <div className="px-5 py-4">
            {pillars.loading ? (
              <Skeleton className="h-16 w-full" />
            ) : (pillars.data ?? []).length === 0 ? (
              <p className="text-[11px] text-steel-600">No pillars defined.</p>
            ) : (
              <ul className="space-y-2">
                {(pillars.data ?? []).map((pillar) => (
                  <li key={pillar.id} className="rounded-md border border-white/[0.05] px-2.5 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-[11px] text-steel-300">{pillar.name}</p>
                      <Mono>w{pillar.weight}</Mono>
                    </div>
                    {pillar.platforms.length ? (
                      <p className="mt-0.5 hq-mono">{pillar.platforms.join(', ')}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            {canEdit ? (
              <div className="mt-3 flex gap-2">
                <Input
                  value={newPillar}
                  placeholder="New pillar"
                  onChange={(event) => setNewPillar(event.target.value)}
                  className="py-1.5 text-[11px]"
                />
                <Button
                  size="sm"
                  loading={addPillar.pending}
                  onClick={async () => {
                    if (!newPillar.trim()) return
                    const result = await addPillar.run(undefined as never)
                    if (result) {
                      setNewPillar('')
                      pillars.reload()
                    }
                  }}
                >
                  Add
                </Button>
              </div>
            ) : null}
          </div>
        </Card>

        <Card>
          <CardHeader title="Competitors" subtitle="Observed facts with provenance." />
          <div className="px-5 py-4">
            {competitors.loading ? (
              <Skeleton className="h-16 w-full" />
            ) : (competitors.data ?? []).length === 0 ? (
              <p className="text-[11px] text-steel-600">
                No competitor profiles. Add them via the API so research can cite them.
              </p>
            ) : (
              <ul className="space-y-2">
                {(competitors.data ?? []).map((competitor) => (
                  <li
                    key={competitor.id}
                    className="rounded-md border border-white/[0.05] px-2.5 py-2"
                  >
                    <p className="text-[11px] text-steel-300">{competitor.name}</p>
                    <p className="mt-0.5 text-[10px] text-steel-600">
                      source: {competitor.source}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <Divider className="!my-2" />
      <SectionTitle>Knowledge base: what is confirmed, and what is not</SectionTitle>
      <Banner tone="warn" title="Only confirmed facts may be used as claims">
        Everything the agents say about the product must trace back to a verified fact. Facts listed
        as needing confirmation are excluded from strategy and content until you confirm them with a
        real source.
      </Banner>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Product facts"
            subtitle="The factual envelope agents are allowed to work inside."
            action={
              <Badge tone={unverifiedFacts.length ? 'warn' : 'ok'}>
                {unverifiedFacts.length} unconfirmed
              </Badge>
            }
          />
          <div className="space-y-2 px-5 py-4">
            {findings.loading ? (
              <SkeletonRows rows={3} height="h-10" />
            ) : verifiedFacts.length === 0 && unverifiedFacts.length === 0 ? (
              <EmptyState
                compact
                title="No product facts recorded"
                description="Seed the knowledge base (python -m app.seed.brand_crusor) or run research to build it."
              />
            ) : (
              <>
                {verifiedFacts.map((fact) => (
                  <div
                    key={fact.id}
                    className="rounded-md border border-signal-ok/20 bg-signal-ok/[0.04] px-2.5 py-2"
                  >
                    <div className="flex items-start gap-2">
                      <Badge tone="ok">confirmed</Badge>
                      <p className="text-[11px] leading-relaxed text-steel-300">{fact.finding}</p>
                    </div>
                    {fact.source_url && (
                      <a
                        href={fact.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="mt-1 block truncate text-[10px] text-violet-300 hover:text-violet-200"
                      >
                        {fact.source_url}
                      </a>
                    )}
                  </div>
                ))}
                {unverifiedFacts.map((fact) => (
                  <div
                    key={fact.id}
                    className="rounded-md border border-signal-warn/20 bg-signal-warn/[0.04] px-2.5 py-2"
                  >
                    <div className="flex items-start gap-2">
                      <Badge tone="warn">needs confirmation</Badge>
                      <div>
                        <p className="text-[11px] leading-relaxed text-steel-300">{fact.finding}</p>
                        {fact.evidence_summary && (
                          <p className="mt-1 text-[10px] leading-relaxed text-steel-600">
                            {fact.evidence_summary}
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Competitors and their evidence"
            subtitle="A name with no evidence cannot support a comparative claim."
          />
          <div className="space-y-2 px-5 py-4">
            {evidence.loading ? (
              <Skeleton className="h-16 w-full" />
            ) : (evidence.data ?? []).length === 0 ? (
              <p className="text-[11px] text-steel-600">No competitors recorded.</p>
            ) : (
              evidence.data!.map((competitor) => (
                <div
                  key={competitor.name}
                  className="flex items-center gap-2 rounded-md border border-white/[0.05] px-2.5 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-[11px] text-steel-300">{competitor.name}</p>
                    {!competitor.positioning && (
                      <p className="text-[10px] text-signal-warn/80">no positioning verified</p>
                    )}
                  </div>
                  <Badge tone={competitor.findings > 0 ? 'ok' : 'muted'} className="ml-auto">
                    {competitor.findings} finding{competitor.findings === 1 ? '' : 's'}
                  </Badge>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Audience segments"
            subtitle="From the founder brief. Motivations are assumptions until research confirms them."
          />
          <div className="space-y-2 px-5 py-4">
            {audienceSegments.length === 0 ? (
              <p className="text-[11px] text-steel-600">
                Segments are derived from the profile audience list above.
              </p>
            ) : (
              audienceSegments.map((segment) => (
                <div key={segment} className="rounded-md border border-white/[0.05] px-2.5 py-2">
                  <p className="text-[11px] text-steel-300">{segment}</p>
                  <p className="mt-0.5 text-[10px] text-steel-600">
                    stated audience — confirm motivation with research
                  </p>
                </div>
              ))
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Business objectives"
            subtitle="Strategies must map to at least one of the five approved outcomes."
          />
          <div className="space-y-1.5 px-5 py-4">
            {[
              'Developer acquisition (primary)',
              'Waitlist registrations',
              'Product adoption',
              'Community growth',
              'Brand awareness (supporting only)',
            ].map((objective) => (
              <div key={objective} className="flex items-center gap-2">
                <span className="h-1 w-1 rounded-full bg-violet-400/70" />
                <span className="text-[11px] text-steel-400">{objective}</span>
              </div>
            ))}
            <p className="mt-2 border-t border-white/[0.05] pt-2 text-[10px] leading-relaxed text-steel-600">
              Follower counts, likes and impressions are recorded as context but are never treated
              as the success criterion.
            </p>
          </div>
        </Card>
      </div>

      {unverifiedClaims.length > 0 && (
        <Card>
          <CardHeader
            title="Unverified claims pending founder confirmation"
            subtitle="Excluded from generated strategies and content."
            action={<Badge tone="warn">{unverifiedClaims.length}</Badge>}
          />
          <ul className="space-y-1.5 px-5 py-4">
            {unverifiedClaims.map((claim, index) => (
              <li key={index} className="flex items-start gap-2">
                <Badge tone="warn">unconfirmed</Badge>
                <span className="text-[11px] leading-relaxed text-steel-400">{claim}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

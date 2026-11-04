import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { contentApi, contentStudioApi } from '../lib/api'
import type {
  ContentVariant,
  ContentVersion,
  PlatformSpec,
  ValidationResult,
  VariantSet,
} from '../lib/types'
import { formatDateTime, titleCase } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Input,
  KeyValue,
  SkeletonRows,
  StatusBadge,
  Textarea,
  VerdictBadge,
} from '../components/ui'
import { PlatformChip } from '../components/shared'

const VERDICT_TONE: Record<string, 'ok' | 'warn' | 'danger' | 'muted'> = {
  PASS: 'ok',
  PASS_WITH_WARNINGS: 'warn',
  NEEDS_REVISION: 'warn',
  BLOCKED: 'danger',
}

function countChars(value: string) {
  return value.trim().length
}

function ValidationPanel({ validation }: { validation: ValidationResult | null }) {
  if (!validation) {
    return (
      <p className="text-[11px] leading-relaxed text-steel-600">
        No validation recorded for this version. Run the Brand Guardian to check claims, voice and
        platform formatting.
      </p>
    )
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={VERDICT_TONE[validation.verdict] ?? 'neutral'}>
          {validation.verdict.replace(/_/g, ' ')}
        </Badge>
        <span className="text-[11px] text-steel-500">score {validation.score}</span>
        <span className="text-[11px] text-steel-600">
          {validation.blocking_count} blocking · {validation.warning_count} warning
        </span>
        {validation.requires_founder_confirmation ? (
          <Badge tone="violet">founder confirmation needed</Badge>
        ) : null}
      </div>
      <p className="text-[10px] uppercase tracking-wide text-steel-600">
        Rules checked: {validation.checked_rules.join(', ')}
      </p>
      {validation.issues.length === 0 ? (
        <p className="text-[11px] text-steel-500">No issues found.</p>
      ) : (
        <ul className="space-y-2">
          {validation.issues.map((issue, index) => (
            <li
              key={`${issue.rule}-${index}`}
              className="rounded-md border border-white/[0.06] px-3 py-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={issue.severity === 'blocking' ? 'danger' : 'warn'}>
                  {issue.severity}
                </Badge>
                <span className="text-[11px] font-medium text-steel-400">{issue.rule}</span>
                <span className="text-[10px] uppercase tracking-wide text-steel-600">
                  {issue.category}
                </span>
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-steel-400">{issue.message}</p>
              {issue.affected_text ? (
                <p className="mt-1 rounded bg-white/[0.03] px-2 py-1 font-mono text-[10px] text-steel-500">
                  {issue.affected_text}
                </p>
              ) : null}
              {issue.explanation ? (
                <p className="mt-1 text-[11px] leading-relaxed text-steel-600">{issue.explanation}</p>
              ) : null}
              {issue.suggestion ? (
                <p className="mt-1 text-[11px] leading-relaxed text-steel-500">
                  Suggested correction: {issue.suggestion}
                </p>
              ) : null}
              {issue.requires_founder_confirmation ? (
                <p className="mt-1 text-[11px] text-violet-300">
                  Requires founder confirmation before approval.
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function VariantComparison({
  variantSet,
  onSelect,
  pending,
  error,
}: {
  variantSet: VariantSet | null
  onSelect: (index: number) => void
  pending: boolean
  error: string | null
}) {
  if (!variantSet) return null
  return (
    <Card>
      <CardHeader
        title="Variant comparison"
        subtitle={`${variantSet.variants.length} variants from ${variantSet.provider} · different angles, not reworded copies`}
        action={variantSet.is_mock ? <Badge tone="violet">mock</Badge> : null}
      />
      {error ? <div className="mb-3"><ErrorState message={error} /></div> : null}
      <div className="space-y-3">
        {variantSet.variants.map((variant: ContentVariant) => (
          <div key={variant.index} className="rounded-lg border border-white/[0.06] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-medium text-steel-400">
                  Variant {variant.index + 1}
                </span>
                <Badge tone={VERDICT_TONE[variant.validation.status] ?? 'neutral'}>
                  {variant.validation.status.replace(/_/g, ' ')}
                </Badge>
                <span className="text-[10px] text-steel-600">{variant.character_count} chars</span>
                {variantSet.selected_variant === variant.index ? (
                  <Badge tone="ok">on this version</Badge>
                ) : null}
              </div>
              <Button
                variant="ghost"
                onClick={() => onSelect(variant.index)}
                disabled={pending || variantSet.selected_variant === variant.index}
              >
                Use this variant
              </Button>
            </div>
            {variant.hook ? (
              <p className="mt-2 text-xs font-medium text-steel-300">{variant.hook}</p>
            ) : null}
            <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-steel-400">
              {variant.body}
            </p>
            {variant.call_to_action ? (
              <p className="mt-2 text-[11px] text-steel-500">CTA: {variant.call_to_action}</p>
            ) : null}
            {variant.validation.status === 'BLOCKED' ? (
              <p className="mt-2 text-[11px] text-signal-danger">
                Blocked — selecting this variant keeps the draft out of approval.
              </p>
            ) : null}
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11px] leading-relaxed text-steel-600">
        Selecting a variant creates a new version. If the item was approved, that approval is
        invalidated and the earlier version stays in history untouched.
      </p>
    </Card>
  )
}

export function ContentDraftEditorPage() {
  const { id = '' } = useParams()
  const item = useApi(() => contentApi.get(id), [id])
  const versions = useApi(() => contentApi.versions(id), [id])
  const validations = useApi(() => contentStudioApi.validations(id), [id])
  const platforms = useApi(() => contentStudioApi.platforms())
  const variants = useApi(() => contentStudioApi.variants(id), [id], {
    enabled: Boolean(id),
  })

  const [headline, setHeadline] = useState('')
  const [body, setBody] = useState('')
  const [cta, setCta] = useState('')
  const [hashtags, setHashtags] = useState('')
  const [changeNote, setChangeNote] = useState('')
  const [compareId, setCompareId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const current = item.data?.current_version ?? null

  useEffect(() => {
    if (!current) return
    setHeadline(current.headline ?? '')
    setBody(current.body ?? '')
    setCta(current.call_to_action ?? '')
    setHashtags((current.hashtags ?? []).join(', '))
  }, [current?.id])

  const spec: PlatformSpec | undefined = useMemo(
    () => (platforms.data ?? []).find((entry) => entry.platform === item.data?.platform),
    [platforms.data, item.data?.platform],
  )

  const save = useMutation(async () =>
    contentApi.createVersion(id, {
      headline,
      body,
      call_to_action: cta,
      hashtags: hashtags
        .split(',')
        .map((tag) => tag.trim().replace(/^#/, ''))
        .filter(Boolean),
      change_note: changeNote || 'Edited in Content Studio',
    }),
  )

  const validate = useMutation(async () => contentStudioApi.validate(id))
  const select = useMutation(async (index: number) => contentStudioApi.selectVariant(id, index))
  const regenerate = useMutation(async () => contentStudioApi.regenerateVariants(id, 2))

  const dirty =
    current !== null &&
    (body !== (current.body ?? '') ||
      headline !== (current.headline ?? '') ||
      cta !== (current.call_to_action ?? '') ||
      hashtags !== (current.hashtags ?? []).join(', '))

  const onSave = async () => {
    const result = await save.run(undefined as never)
    if (result) {
      setChangeNote('')
      setNotice(`Saved as version ${result.current_version?.version_number ?? 'new'}.`)
      item.reload()
      versions.reload()
      validations.reload()
      variants.reload()
    }
  }

  const onValidate = async () => {
    const result = await validate.run(undefined as never)
    if (result) {
      setNotice(`Brand Guardian verdict: ${result.verdict.replace(/_/g, ' ')}.`)
      validations.reload()
      item.reload()
    }
  }

  const onSelect = async (index: number) => {
    const result = await select.run(index)
    if (result) {
      setNotice(
        result.approval_invalidated
          ? `Variant applied as version ${result.version_number}; the previous approval was invalidated.`
          : `Variant applied as version ${result.version_number}.`,
      )
      item.reload()
      versions.reload()
      validations.reload()
      variants.reload()
    }
  }

  const onRegenerate = async () => {
    const result = await regenerate.run(undefined as never)
    if (result) {
      setNotice(
        `${result.variants.length} fresh variants written to version ${result.version_number}` +
          (result.approvals_invalidated
            ? '; the previous approval was invalidated and its record kept.'
            : '.'),
      )
      item.reload()
      versions.reload()
      validations.reload()
      variants.reload()
    }
  }

  if (item.loading) return <SkeletonRows rows={8} />
  if (item.error) return <ErrorState message={item.error.message} onRetry={item.reload} />
  if (!item.data) return <EmptyState title="Draft not found" description="It may have been removed." />

  const latestValidation: ValidationResult | null = validations.data?.[0] ?? null
  const compareVersion: ContentVersion | undefined = (versions.data ?? []).find(
    (version) => version.id === compareId,
  )
  const bodyLimit = spec?.body_max_chars ?? 0

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Content Studio"
        title={item.data.title}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <PlatformChip platform={item.data.platform} />
            <StatusBadge status={item.data.status} />
            <Badge tone="muted">{titleCase(item.data.format ?? '')}</Badge>
            <span>
              {item.data.version_count} version{item.data.version_count === 1 ? '' : 's'} ·{' '}
              {item.data.has_valid_approval ? 'approval valid' : 'no valid approval'}
            </span>
          </span>
        }
        actions={
          <Link
            to={`/calendar`}
            className="text-[11px] text-violet-300 hover:text-violet-200"
          >
            Internal calendar →
          </Link>
        }
      />

      {notice ? <Banner tone="info">{notice}</Banner> : null}
      {item.data.status === 'APPROVED' ? (
        <Banner tone="warn" title="Approved version">
          Editing this draft creates a new version and invalidates the current approval. The
          approved version is never rewritten.
        </Banner>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1.15fr_0.85fr]">
        <div className="space-y-4">
          <Card>
            <CardHeader title="Draft editor" subtitle="Every save creates a new immutable version." />
            <div className="space-y-3">
              <Input
                label="Headline / hook"
                value={headline}
                onChange={(event) => setHeadline(event.target.value)}
                hint={
                  spec
                    ? `${countChars(headline)}/${spec.hook_max_chars} characters`
                    : undefined
                }
              />
              <Textarea
                label="Body"
                rows={12}
                value={body}
                onChange={(event) => setBody(event.target.value)}
                hint={
                  spec
                    ? `${countChars(body)}/${spec.body_max_chars} characters allowed by ${spec.label}`
                    : undefined
                }
              />
              <Input
                label="Call to action"
                value={cta}
                onChange={(event) => setCta(event.target.value)}
                hint={spec?.cta_guidance}
              />
              <Input
                label="Hashtags (comma separated)"
                value={hashtags}
                onChange={(event) => setHashtags(event.target.value)}
                hint={spec?.hashtag_guidance}
              />
              <Input
                label="Change note"
                value={changeNote}
                onChange={(event) => setChangeNote(event.target.value)}
                placeholder="What changed?"
              />
            </div>
            {save.error ? <div className="mt-3"><ErrorState message={save.error.message} /></div> : null}
            <div className="mt-4 flex flex-wrap gap-2">
              <Button onClick={onSave} disabled={save.pending || !dirty}>
                {save.pending ? 'Saving…' : dirty ? 'Save as new version' : 'No changes'}
              </Button>
              <Button variant="ghost" onClick={onValidate} disabled={validate.pending}>
                {validate.pending ? 'Validating…' : 'Run Brand Guardian'}
              </Button>
              <Button variant="ghost" onClick={onRegenerate} disabled={regenerate.pending}>
                {regenerate.pending ? 'Generating…' : 'Regenerate variants'}
              </Button>
            </div>
            {validate.error ? <div className="mt-3"><ErrorState message={validate.error.message} /></div> : null}
            {regenerate.error ? <div className="mt-3"><ErrorState message={regenerate.error.message} /></div> : null}
          </Card>

          <Card>
            <CardHeader
              title="Version history"
              subtitle="Approved versions are immutable; edits never overwrite them."
            />
            {versions.loading ? (
              <SkeletonRows rows={3} />
            ) : versions.error ? (
              <ErrorState message={versions.error.message} onRetry={versions.reload} />
            ) : !versions.data?.length ? (
              <EmptyState title="No versions" description="This draft has no saved versions." />
            ) : (
              <ul className="divide-y divide-white/[0.05]">
                {[...versions.data]
                  .sort((a, b) => b.version_number - a.version_number)
                  .map((version) => (
                    <li key={version.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-medium text-steel-300">
                            v{version.version_number}
                          </span>
                          {version.id === current?.id ? <Badge tone="ok">current</Badge> : null}
                          {version.id === compareId ? <Badge tone="violet">comparing</Badge> : null}
                          {version.created_by_agent === 'content' ? (
                            <Badge tone="muted">agent</Badge>
                          ) : null}
                        </div>
                        <p className="mt-0.5 truncate text-[11px] text-steel-600">
                          {version.change_note || 'No change note'} ·{' '}
                          {formatDateTime(version.created_at)}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="ghost"
                          onClick={() => setCompareId(version.id === compareId ? null : version.id)}
                        >
                          {version.id === compareId ? 'Hide' : 'Compare'}
                        </Button>
                        {version.id !== current?.id ? (
                          <Button
                            variant="ghost"
                            onClick={() => {
                              setHeadline(version.headline ?? '')
                              setBody(version.body ?? '')
                              setCta(version.call_to_action ?? '')
                              setHashtags((version.hashtags ?? []).join(', '))
                              setChangeNote(`Restored content from v${version.version_number}`)
                            }}
                          >
                            Load into editor
                          </Button>
                        ) : null}
                      </div>
                    </li>
                  ))}
              </ul>
            )}

            {compareVersion ? (
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div className="rounded-lg border border-white/[0.06] p-3">
                  <p className="hq-label mb-1">v{compareVersion.version_number}</p>
                  <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-steel-500">
                    {compareVersion.body || '—'}
                  </p>
                </div>
                <div className="rounded-lg border border-white/[0.06] p-3">
                  <p className="hq-label mb-1">v{current?.version_number ?? '—'} (current edit)</p>
                  <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-steel-400">
                    {body || '—'}
                  </p>
                </div>
              </div>
            ) : null}
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Preview"
              subtitle={spec ? `${spec.label} formatting rules` : 'Platform spec unavailable'}
            />
            <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] p-3">
              <p className="text-xs font-medium text-steel-300">{headline || 'Untitled'}</p>
              <p className="mt-2 whitespace-pre-wrap text-[11px] leading-relaxed text-steel-400">
                {body || 'No body yet.'}
              </p>
              {cta ? <p className="mt-2 text-[11px] text-steel-500">{cta}</p> : null}
              {hashtags ? <p className="mt-2 text-[11px] text-violet-300">{hashtags}</p> : null}
            </div>
            {spec ? (
              <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
                <KeyValue label="Body limit">{spec.body_max_chars}</KeyValue>
                <KeyValue label="Hashtags">
                  {spec.hashtag_min}–{spec.hashtag_max}
                </KeyValue>
                <KeyValue label="Tone">{spec.tone}</KeyValue>
                <KeyValue label="Community first">{spec.community_first ? 'yes' : 'no'}</KeyValue>
              </dl>
            ) : null}
            {spec && bodyLimit > 0 && countChars(body) > bodyLimit ? (
              <p className="mt-2 text-[11px] text-signal-warn">
                Body exceeds the {bodyLimit}-character limit for {spec.label}.
              </p>
            ) : null}
          </Card>

          <Card>
            <CardHeader
              title="Brand Guardian"
              subtitle="Product accuracy, unsupported claims, voice, formatting"
              action={<VerdictBadge verdict={latestValidation?.verdict ?? null} />}
            />
            {validations.loading ? (
              <SkeletonRows rows={3} />
            ) : validations.error ? (
              <ErrorState message={validations.error.message} onRetry={validations.reload} />
            ) : (
              <ValidationPanel validation={latestValidation} />
            )}
            {validations.data && validations.data.length > 1 ? (
              <p className="mt-3 text-[10px] uppercase tracking-wide text-steel-600">
                {validations.data.length} validations recorded for this draft · newest verdict{' '}
                {validations.data[0].verdict.replace(/_/g, ' ')}
              </p>
            ) : null}
          </Card>

          <Card>
            <CardHeader title="Approval" subtitle="Approval binds to an exact version" />
            <p className="text-[11px] leading-relaxed text-steel-600">
              Moving this draft through review, approval or changes happens in the Approval Centre
              so every decision is recorded against a specific version.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Link
                to="/approvals"
                className="rounded-md border border-white/[0.1] px-3 py-1.5 text-[11px] text-steel-400 hover:text-steel-300"
              >
                Open Approval Centre
              </Link>
              <Link
                to={`/content/${id}`}
                className="rounded-md border border-white/[0.1] px-3 py-1.5 text-[11px] text-steel-400 hover:text-steel-300"
              >
                Full content record
              </Link>
            </div>
          </Card>
        </div>
      </div>

      {variants.data && variants.data.variants.length === 0 ? (
        <Banner tone="neutral" title="No variants stored on this version">
          Variants are kept with the version that produced them, so a hand-edited version has
          none. Use “Regenerate variants” to write a new set to a new version.
        </Banner>
      ) : null}

      <VariantComparison
        variantSet={variants.data}
        onSelect={onSelect}
        pending={select.pending}
        error={select.error?.message ?? variants.error?.message ?? null}
      />
    </div>
  )
}

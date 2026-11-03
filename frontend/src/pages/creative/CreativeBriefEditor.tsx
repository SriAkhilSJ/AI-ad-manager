import { useMemo, useState } from 'react'
import { useApi, useMutation } from '../../hooks/useApi'
import { useAuth } from '../../hooks/useAuth'
import { useToast } from '../../hooks/useToast'
import { contentApi, creativeStudioApi } from '../../lib/api'
import type { CreativeBrief } from '../../lib/types'
import { formatDateTime } from '../../lib/format'
import { ErrorState } from '../../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  Divider,
  EmptyState,
  KeyValue,
  Mono,
  SectionTitle,
  Select,
  Skeleton,
  Textarea,
} from '../../components/ui'

/**
 * Brief editor.
 *
 * Creating a brief is a *read* of the content version: the backend records the
 * content hash it briefed against and never writes to the content item. The UI
 * says so, and shows the hash the brief was built from.
 */
export function CreativeBriefEditor() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const [contentItemId, setContentItemId] = useState('')
  const [assetType, setAssetType] = useState<'image' | 'video'>('image')
  const [direction, setDirection] = useState('')
  const [selectedBriefId, setSelectedBriefId] = useState<string | null>(null)

  const content = useApi(() => contentApi.list({ limit: 50 }))
  const briefs = useApi(() => creativeStudioApi.briefs({ limit: 50 }), [selectedBriefId])

  const create = useMutation(async () => {
    if (!contentItemId) throw new Error('Choose a content item first.')
    const item = (content.data ?? []).find((row) => row.id === contentItemId)
    if (!item) throw new Error('That content item is no longer in the list.')
    if (!item.current_version_id) throw new Error('That content item has no current version.')
    return creativeStudioApi.createBrief({
      content_version_id: item.current_version_id,
      asset_type: assetType,
      creative_direction: direction,
    })
  })

  async function submit() {
    const result = await create.run(undefined as never)
    if (result) {
      notify(
        result.version_number > 1
          ? `Created brief v${result.version_number}.`
          : 'Brief created from the current content version.',
        'ok',
      )
      setSelectedBriefId(result.id)
      briefs.reload()
    }
  }

  const items = content.data ?? []
  const chosen = useMemo(
    () => items.find((row) => row.id === contentItemId) ?? null,
    [items, contentItemId],
  )
  const selected = useMemo(
    () => (briefs.data ?? []).find((brief) => brief.id === selectedBriefId) ?? null,
    [briefs.data, selectedBriefId],
  )

  return (
    <div className="grid gap-4 xl:grid-cols-5">
      <div className="space-y-4 xl:col-span-2">
        <Card>
          <CardHeader
            title="New brief"
            subtitle="A brief binds one content version, one platform and one format."
          />
          <div className="space-y-3 px-5 py-4">
            {content.error ? (
              <ErrorState message={content.error.message} onRetry={content.reload} />
            ) : content.loading ? (
              <Skeleton className="h-24 w-full" />
            ) : items.length === 0 ? (
              <EmptyState
                compact
                title="No content to brief"
                description="Create and approve a content draft in Content Studio first."
              />
            ) : (
              <>
                <label className="block">
                  <span className="hq-label">Content item</span>
                  <Select
                    className="mt-1 w-full"
                    value={contentItemId}
                    onChange={(event) => setContentItemId(event.target.value)}
                  >
                    <option value="">Select a content item…</option>
                    {items.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.title} · {item.platform} · {item.status}
                      </option>
                    ))}
                  </Select>
                </label>

                {chosen ? (
                  <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2">
                    <KeyValue label="Platform">
                      <Badge tone="muted">{chosen.platform}</Badge>
                    </KeyValue>
                    <KeyValue label="Status">
                      <Badge tone={chosen.status === 'APPROVED' ? 'ok' : 'warn'}>{chosen.status}</Badge>
                    </KeyValue>
                    <KeyValue label="Version">
                      <Mono>{chosen.current_version_id?.slice(0, 8) ?? '—'}</Mono>
                    </KeyValue>
                  </div>
                ) : null}

                <label className="block">
                  <span className="hq-label">Asset type</span>
                  <Select
                    className="mt-1 w-full"
                    value={assetType}
                    onChange={(event) => setAssetType(event.target.value as 'image' | 'video')}
                  >
                    <option value="image">Image</option>
                    <option value="video">Video</option>
                  </Select>
                </label>

                <label className="block">
                  <span className="hq-label">Creative direction (optional)</span>
                  <Textarea
                    className="mt-1 w-full"
                    rows={3}
                    value={direction}
                    placeholder="e.g. a developer at a terminal, charcoal frame, single silver geometric accent"
                    onChange={(event) => setDirection(event.target.value)}
                  />
                </label>

                <Button
                  variant="primary"
                  loading={create.pending}
                  disabled={!canEdit || !contentItemId}
                  onClick={submit}
                >
                  Build brief
                </Button>
                {create.error ? (
                  <p className="text-[11px] text-signal-danger">{create.error.message}</p>
                ) : null}
                <p className="text-[11px] leading-relaxed text-steel-600">
                  Building a brief reads the content version and records its hash. The content itself
                  is never modified.
                </p>
              </>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader title="Brief versions" subtitle="Newest first" />
          {briefs.error ? (
            <div className="p-4">
              <ErrorState message={briefs.error.message} onRetry={briefs.reload} />
            </div>
          ) : briefs.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : (briefs.data ?? []).length === 0 ? (
            <EmptyState compact title="No briefs yet" description="Build one above." />
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {(briefs.data ?? []).map((brief) => (
                <li key={brief.id}>
                  <button
                    className={
                      'flex w-full items-center justify-between gap-3 px-5 py-3 text-left transition-colors hover:bg-white/[0.03]' +
                      (selectedBriefId === brief.id ? ' bg-white/[0.04]' : '')
                    }
                    onClick={() => setSelectedBriefId(brief.id)}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-xs text-steel-300">
                        v{brief.version_number} · {brief.asset_type} · {brief.platform}
                      </span>
                      <span className="mt-0.5 block truncate text-[11px] text-steel-600">
                        {brief.aspect_ratio} · {brief.resolution} · {formatDateTime(brief.created_at)}
                      </span>
                    </span>
                    <Mono>{brief.brief_hash.slice(0, 8)}</Mono>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="xl:col-span-3">
        {selected ? (
          <BriefDetail brief={selected} />
        ) : (
          <Card className="h-full">
            <EmptyState
              title="Select a brief"
              description="Every field the generator will use is shown here, including the negative prompt and the constraints Creative QA will enforce."
            />
          </Card>
        )}
      </div>
    </div>
  )
}

function BriefList({ title, values }: { title: string; values: string[] }) {
  if (!values || values.length === 0) return null
  return (
    <div>
      <SectionTitle>{title}</SectionTitle>
      <ul className="list-disc space-y-1 pl-4 text-xs leading-relaxed text-steel-400">
        {values.map((value) => (
          <li key={value}>{value}</li>
        ))}
      </ul>
    </div>
  )
}

function BriefDetail({ brief }: { brief: CreativeBrief }) {
  return (
    <Card>
      <CardHeader
        title={`Brief v${brief.version_number}`}
        subtitle={`${brief.platform} · ${brief.asset_type} · ${brief.aspect_ratio} · ${brief.resolution}`}
        action={<Badge tone="violet">{brief.content_hash.slice(0, 10)}</Badge>}
      />
      <div className="space-y-4 px-5 py-4">
        <div>
          <SectionTitle>Creative objective</SectionTitle>
          <p className="text-xs leading-relaxed text-steel-300">{brief.creative_objective}</p>
        </div>
        <div>
          <SectionTitle>Visual concept</SectionTitle>
          <p className="text-xs leading-relaxed text-steel-300">{brief.visual_concept}</p>
        </div>

        <Divider />

        <div className="grid gap-3 md:grid-cols-2">
          <KeyValue label="Background">{brief.background}</KeyValue>
          <KeyValue label="Lighting">{brief.lighting}</KeyValue>
          <KeyValue label="Duration">
            {brief.duration_seconds ? `${brief.duration_seconds}s` : '—'}
          </KeyValue>
          <KeyValue label="Aspect">{brief.aspect_ratio}</KeyValue>
        </div>

        <BriefList title="Subjects" values={brief.subjects} />
        <BriefList title="Scenes" values={brief.scenes} />
        <BriefList title="Composition" values={brief.composition} />
        <BriefList title="Typography" values={brief.typography} />
        <BriefList title="Brand asset requirements" values={brief.brand_asset_requirements} />
        <BriefList title="Reference assets" values={brief.reference_assets} />

        <div>
          <SectionTitle>Colour palette</SectionTitle>
          <div className="flex flex-wrap gap-2">
            {Object.entries(brief.colour_palette ?? {}).map(([role, value]) => (
              <span
                key={role}
                className="flex items-center gap-2 rounded-md border border-white/[0.08] px-2 py-1 text-[11px] text-steel-400"
              >
                <span
                  className="inline-block h-3 w-3 rounded-sm border border-white/10"
                  style={{ background: /^#|rgb/.test(String(value)) ? String(value) : 'transparent' }}
                />
                {role.replace(/_/g, ' ')}: <Mono>{String(value)}</Mono>
              </span>
            ))}
          </div>
        </div>

        <div>
          <SectionTitle>Image prompt</SectionTitle>
          <p className="hq-mono whitespace-pre-wrap rounded-lg border border-white/[0.06] bg-black/30 p-3 text-[11px] leading-relaxed text-steel-400">
            {brief.image_prompt}
          </p>
        </div>
        <BriefList title="Negative prompt" values={brief.negative_prompt} />

        <div>
          <SectionTitle>Validation constraints</SectionTitle>
          <div className="space-y-1">
            {Object.entries(brief.validation_constraints ?? {}).map(([key, value]) => (
              <KeyValue key={key} label={key.replace(/_/g, ' ')}>
                <Mono>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</Mono>
              </KeyValue>
            ))}
          </div>
        </div>

        <Banner tone="neutral">
          Creative QA will check this brief's constraints automatically. Text readability, aesthetics
          and on-brand judgement are <strong>not</strong> automated — a human still has to look.
        </Banner>

        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="muted">brief {brief.id.slice(0, 8)}</Badge>
          <Badge tone="muted">hash {brief.brief_hash.slice(0, 12)}</Badge>
          <Badge tone="muted">created {formatDateTime(brief.created_at)}</Badge>
        </div>
      </div>
    </Card>
  )
}

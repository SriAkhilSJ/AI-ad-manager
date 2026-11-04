import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useApi, useMutation } from '../../hooks/useApi'
import { useAuth } from '../../hooks/useAuth'
import { useToast } from '../../hooks/useToast'
import { creativeStudioApi } from '../../lib/api'
import type { CreativeQaCheck, CreativeQaIssue, CreativeQaReport } from '../../lib/types'
import { formatBytes, formatDateTime } from '../../lib/format'
import { ErrorState, PageHeader } from '../../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  Divider,
  KeyValue,
  Mono,
  SectionTitle,
  Skeleton,
} from '../../components/ui'
import { validationTone } from './CreativeAssetLibrary'
import { useAssetPreview } from './useAssetPreview'

function asReport(value: unknown): CreativeQaReport | null {
  if (!value || typeof value !== 'object') return null
  const report = value as CreativeQaReport
  return Array.isArray(report.checks) ? report : null
}

function QaCheckRow({ check }: { check: CreativeQaCheck }) {
  const tone = !check.performed ? 'muted' : check.passed ? 'ok' : 'danger'
  const label = !check.performed ? 'not performed' : check.passed ? 'pass' : 'fail'
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <div className="min-w-0">
        <p className="text-[11px] text-steel-300">
          {check.rule.replace(/_/g, ' ')}{' '}
          <span className="text-steel-600">· {check.category}</span>
        </p>
        <p className="mt-0.5 text-[11px] leading-relaxed text-steel-600">{check.detail}</p>
      </div>
      <Badge tone={tone}>{label}</Badge>
    </div>
  )
}

function IssueRow({ issue }: { issue: CreativeQaIssue }) {
  const tone =
    issue.severity === 'blocking'
      ? 'danger'
      : issue.severity === 'needs_revision'
        ? 'warn'
        : 'muted'
  return (
    <div className="rounded-lg border border-white/[0.06] px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[11px] font-medium text-steel-300">
          {issue.rule.replace(/_/g, ' ')}
        </span>
        <Badge tone={tone}>{issue.severity.replace(/_/g, ' ')}</Badge>
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-steel-400">{issue.message}</p>
      {issue.suggestion ? (
        <p className="mt-1 text-[11px] leading-relaxed text-steel-600">→ {issue.suggestion}</p>
      ) : null}
    </div>
  )
}

export function CreativeAssetDetailPage() {
  const { assetId } = useParams()
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const versionRef = useRef<HTMLInputElement>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const asset = useApi(() => creativeStudioApi.asset(assetId ?? ''), [assetId], {
    enabled: Boolean(assetId),
  })
  const preview = useAssetPreview(assetId)

  const validate = useMutation(async () => creativeStudioApi.validateAsset(assetId ?? ''))
  const select = useMutation(async () => creativeStudioApi.selectAsset(assetId ?? ''))
  const uploadVersion = useMutation(async () => {
    const file = versionRef.current?.files?.[0]
    if (!file) throw new Error('Choose a file first.')
    return creativeStudioApi.uploadVersion(assetId ?? '', file)
  })

  async function runValidate() {
    setActionError(null)
    const result = await validate.run(undefined as never)
    if (result) {
      notify(`Validation: ${result.qa_report.verdict.replace(/_/g, ' ')}.`, 'neutral')
      asset.reload()
    } else {
      setActionError(validate.error?.message ?? 'Validation failed.')
    }
  }

  async function runSelect() {
    setActionError(null)
    const result = await select.run(undefined as never)
    if (result) {
      notify(result.message, result.invalidated_approvals > 0 ? 'neutral' : 'ok')
      asset.reload()
    } else {
      setActionError(select.error?.message ?? 'Selection failed.')
    }
  }

  async function runUploadVersion() {
    setActionError(null)
    const result = await uploadVersion.run(undefined as never)
    if (result) {
      notify(result.message, result.invalidated_approvals > 0 ? 'neutral' : 'ok')
      asset.reload()
      if (versionRef.current) versionRef.current.value = ''
    } else {
      setActionError(uploadVersion.error?.message ?? 'Upload failed.')
    }
  }

  if (!assetId) return <ErrorState message="No asset id in the URL." onRetry={() => {}} />
  if (asset.error) return <ErrorState message={asset.error.message} onRetry={asset.reload} />
  if (asset.loading || !asset.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    )
  }

  const data = asset.data
  const report = asReport(data.qa_report)
  const meta = data.metadata_json ?? {}

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Creative asset"
        title={data.filename}
        description={
          <span>
            Version {data.asset_version} · {data.kind} · {data.mime_type} ·{' '}
            {data.width ?? '?'}×{data.height ?? '?'} · {formatBytes(data.size_bytes)}
          </span>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Link to="/creative?tab=assets" className="text-[11px] text-violet-300 hover:text-violet-200">
              ← back to library
            </Link>
            <Button size="sm" variant="secondary" loading={validate.pending} disabled={!canEdit} onClick={runValidate}>
              Re-run QA
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={select.pending}
              disabled={!canEdit || data.selected}
              onClick={runSelect}
            >
              {data.selected ? 'Selected' : 'Select for approval'}
            </Button>
          </div>
        }
      />

      {actionError ? <Banner tone="danger">{actionError}</Banner> : null}
      {data.is_mock ? <Banner tone="warn" title="Mock asset">{data.mock_label}</Banner> : null}

      <div className="grid gap-4 xl:grid-cols-5">
        <div className="space-y-4 xl:col-span-2">
          <Card className="overflow-hidden">
            <div className="flex aspect-[4/5] items-center justify-center bg-black/50">
              {preview.url ? (
                <img src={preview.url} alt={data.filename} className="max-h-full max-w-full object-contain" />
              ) : preview.error ? (
                <p className="text-[11px] text-steel-600">{preview.error}</p>
              ) : (
                <Skeleton className="h-full w-full" />
              )}
            </div>
            <div className="space-y-1 px-5 py-4">
              <KeyValue label="Checksum">
                <Mono>{data.checksum.slice(0, 16)}…</Mono>
              </KeyValue>
              <KeyValue label="Detected type">
                <Mono>{data.detected_mime || '—'}</Mono>
              </KeyValue>
              <KeyValue label="Aspect">{data.aspect_ratio}</KeyValue>
              <KeyValue label="Provider">{data.provider || '—'}</KeyValue>
              <KeyValue label="Tagged kind">{meta.job_type ? String(meta.job_type) : data.kind}</KeyValue>
              <KeyValue label="Selected">
                <Badge tone={data.selected ? 'violet' : 'muted'}>{data.selected ? 'yes' : 'no'}</Badge>
              </KeyValue>
              <KeyValue label="Approval">
                <Badge
                  tone={
                    data.approval_status === 'approved'
                      ? 'ok'
                      : data.approval_status === 'invalidated'
                        ? 'danger'
                        : 'muted'
                  }
                >
                  {data.approval_status.replace(/_/g, ' ')}
                </Badge>
              </KeyValue>
              <KeyValue label="Validation">
                <Badge tone={validationTone(data.validation_status)}>
                  {data.validation_status.replace(/_/g, ' ')}
                </Badge>
              </KeyValue>
              <KeyValue label="Created">{formatDateTime(data.created_at)}</KeyValue>
            </div>
          </Card>

          <Card>
            <CardHeader title="Upload a new version" subtitle="The current bytes stay untouched." />
            <div className="flex flex-wrap items-center gap-2 px-5 py-4">
              <input ref={versionRef} type="file" className="hq-input max-w-[200px] py-1 text-[11px]" />
              <Button
                size="sm"
                variant="danger"
                loading={uploadVersion.pending}
                disabled={!canEdit}
                onClick={runUploadVersion}
              >
                Upload version {data.asset_version + 1}
              </Button>
            </div>
            <p className="px-5 pb-4 text-[11px] leading-relaxed text-steel-600">
              A new version never inherits selection or approval, and any approval bound to the
              previous version is invalidated — the previous approval record is kept for audit.
            </p>
          </Card>
        </div>

        <div className="space-y-4 xl:col-span-3">
          <Card>
            <CardHeader
              title="Creative QA"
              subtitle={
                report
                  ? `${report.performed_checks} automated check(s) performed · ${report.not_performed_checks} not performed`
                  : 'Not validated yet'
              }
              action={
                report ? (
                  <Badge tone={validationTone(report.verdict)}>
                    {report.verdict.replace(/_/g, ' ')} · {report.score}
                  </Badge>
                ) : undefined
              }
            />
            {!report ? (
              <div className="px-5 py-4 text-xs text-steel-500">
                This asset has no QA report. Run the checks to produce one.
              </div>
            ) : (
              <div className="space-y-4 px-5 py-4">
                <Banner tone="neutral" title="What this verdict means">
                  {report.note}
                </Banner>
                {report.issues.length > 0 ? (
                  <div>
                    <SectionTitle>Issues</SectionTitle>
                    <div className="space-y-2">
                      {report.issues.map((issue) => (
                        <IssueRow key={`${issue.rule}-${issue.message}`} issue={issue} />
                      ))}
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-steel-500">No issues recorded by the automated checks.</p>
                )}

                <Divider />

                <div>
                  <SectionTitle>Checks</SectionTitle>
                  <div className="divide-y divide-white/[0.04]">
                    {report.checks.map((check) => (
                      <QaCheckRow key={check.rule} check={check} />
                    ))}
                  </div>
                </div>

                {report.pixel_stats ? (
                  <>
                    <Divider />
                    <div>
                      <SectionTitle>Pixel sample</SectionTitle>
                      <div className="grid gap-2 md:grid-cols-2">
                        {Object.entries(report.pixel_stats).map(([key, value]) => (
                          <KeyValue key={key} label={key.replace(/_/g, ' ')}>
                            <Mono>{String(value)}</Mono>
                          </KeyValue>
                        ))}
                      </div>
                    </div>
                  </>
                ) : null}

                <p className="text-[11px] leading-relaxed text-steel-600">
                  Visual verification: <strong>{report.visual_verification.replace(/_/g, ' ')}</strong>.
                  {report.human_visual_review_required
                    ? ' A human must still review this asset before it is used.'
                    : ''}
                </p>
              </div>
            )}
          </Card>

          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader title="Version history" subtitle={`${data.versions.length} version(s)`} />
              <div className="divide-y divide-white/[0.04]">
                {data.versions.map((version) => (
                  <div key={version.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                    <div>
                      <p className="text-[11px] text-steel-300">
                        v{version.asset_version} · {version.status}
                        {version.selected ? ' · selected' : ''}
                        {version.is_mock ? ' · mock' : ''}
                      </p>
                      <p className="mt-0.5 text-[10px] text-steel-600">
                        {formatDateTime(version.created_at)} · {formatBytes(version.size_bytes)}
                      </p>
                    </div>
                    {version.id === data.id ? (
                      <Badge tone="violet">current</Badge>
                    ) : (
                      <Link
                        to={`/creative/assets/${version.id}`}
                        className="text-[11px] text-violet-300 hover:text-violet-200"
                      >
                        open
                      </Link>
                    )}
                  </div>
                ))}
              </div>
            </Card>

            <Card>
              <CardHeader title="Lineage" subtitle="What produced this asset" />
              <div className="space-y-1 px-5 py-4">
                <KeyValue label="Brief">
                  {data.brief ? (
                    <Mono>
                      v{data.brief.version_number} · {data.brief.platform}
                    </Mono>
                  ) : (
                    '—'
                  )}
                </KeyValue>
                <KeyValue label="Job">
                  {data.job ? <Mono>{data.job.status}</Mono> : '—'}
                </KeyValue>
                <KeyValue label="Content version">
                  {data.content_version_id ? (
                    <Link
                      to={`/content/${data.content_item_id ?? ''}`}
                      className="text-violet-300 hover:text-violet-200"
                    >
                      <Mono>{data.content_version_id.slice(0, 8)}</Mono>
                    </Link>
                  ) : (
                    '—'
                  )}
                </KeyValue>
                <KeyValue label="Campaign">
                  {data.campaign_id ? <Mono>{data.campaign_id.slice(0, 8)}</Mono> : '—'}
                </KeyValue>
                <KeyValue label="Supersedes">
                  {data.supersedes_asset_id ? (
                    <Mono>{data.supersedes_asset_id.slice(0, 8)}</Mono>
                  ) : (
                    '—'
                  )}
                </KeyValue>
                {data.job ? (
                  <>
                    <Divider />
                    <p className="text-[11px] leading-relaxed text-steel-500">{data.job.submitted_prompt}</p>
                  </>
                ) : null}
              </div>
            </Card>
          </div>
        </div>
      </div>
    </div>
  )
}

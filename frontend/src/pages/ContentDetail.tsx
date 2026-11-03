import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { contentApi, newIdempotencyKey, socialApi } from '../lib/api'
import { formatDateTime, relativeTime } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  SectionTitle,
  Skeleton,
  StatusBadge,
  Textarea,
} from '../components/ui'
import { AgentChip, PlatformChip, StatRow } from '../components/shared'
import { IconLock } from '../components/layout/icons'

export function ContentDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { canApprove, canEdit } = useAuth()
  const { notify } = useToast()

  const item = useApi(() => contentApi.get(id as string), [id], { enabled: Boolean(id) })
  const versions = useApi(() => contentApi.versions(id as string), [id], { enabled: Boolean(id) })
  const approvals = useApi(() => contentApi.approvals(id as string), [id], {
    enabled: Boolean(id),
  })
  const publications = useApi(() => contentApi.publications(id as string), [id], {
    enabled: Boolean(id),
  })
  const accounts = useApi(() => socialApi.accounts(), [])

  const [editBody, setEditBody] = useState('')
  const [editNote, setEditNote] = useState('')
  const [editing, setEditing] = useState(false)

  const edit = useMutation(() =>
    contentApi.createVersion(id as string, {
      body: editBody,
      change_note: editNote || 'Edited in Marketing HQ',
    }),
  )
  const publish = useMutation(() =>
    contentApi.publish(id as string, {}, newIdempotencyKey('publish')),
  )
  const [publishMessage, setPublishMessage] = useState<string | null>(null)
  const cancel = useMutation(() => contentApi.cancel(id as string))

  async function submitEdit() {
    const result = await edit.run(undefined as never)
    if (result) {
      notify('New version created. Any prior approval was invalidated.', 'neutral')
      setEditing(false)
      setEditBody('')
      setEditNote('')
      item.reload()
      versions.reload()
      approvals.reload()
    }
  }

  async function doPublish() {
    setPublishMessage(null)
    const result = await publish.run(undefined as never)
    if (result) {
      setPublishMessage(result.message)
      notify(result.message, 'ok')
      item.reload()
      publications.reload()
    }
  }

  if (item.loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (item.error || !item.data) {
    return <ErrorState message={item.error?.message ?? 'Not found'} onRetry={item.reload} />
  }

  const data = item.data
  const version = data.current_version
  const canPublish = data.status === 'APPROVED' || data.status === 'SCHEDULED'

  return (
    <div className="space-y-5">
      <button
        onClick={() => navigate(-1)}
        className="text-[11px] text-steel-500 hover:text-steel-300"
      >
        ← Back
      </button>

      <PageHeader
        eyebrow={`${data.platform} · ${data.format}`}
        title={data.title}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={data.status} />
            <Mono>v{data.version_count}</Mono>
            {data.has_valid_approval ? (
              <Badge tone="ok" dot>
                approval live
              </Badge>
            ) : (
              <Badge tone="muted" dot>
                no live approval
              </Badge>
            )}
            {data.created_by_agent ? <AgentChip agent={data.created_by_agent} /> : null}
          </span>
        }
        actions={
          canEdit ? (
            <Button variant="secondary" onClick={() => setEditing((value) => !value)}>
              {editing ? 'Close editor' : 'Edit copy'}
            </Button>
          ) : undefined
        }
      />

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <div className="space-y-4">
          <Card>
            <CardHeader
              title={`Current version · ${version?.version_number ?? 1}`}
              subtitle={
                version?.change_note ||
                (version?.generation_metadata?.generation_mode
                  ? `Generated ${String(version.generation_metadata.generation_mode)}`
                  : undefined)
              }
              action={
                version ? <Mono>{formatDateTime(version.created_at)}</Mono> : null
              }
            />
            <div className="space-y-3 px-5 py-4">
              {version?.headline ? (
                <p className="text-base font-medium tracking-tight text-steel-300">
                  {version.headline}
                </p>
              ) : null}
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-steel-400">
                {version?.body || 'No body copy.'}
              </p>
              {version?.call_to_action ? (
                <p className="text-xs text-violet-300">{version.call_to_action}</p>
              ) : null}
              {version?.hashtags?.length ? (
                <p className="text-[11px] text-steel-500">{version.hashtags.join('  ')}</p>
              ) : null}
              <div className="rounded-md border border-white/[0.06] bg-ink-900/60 px-3 py-2">
                <p className="hq-mono break-all">hash {version?.content_hash}</p>
              </div>
            </div>
          </Card>

          {editing ? (
            <Card>
              <CardHeader
                title="Create a new version"
                subtitle="Versions are immutable. Saving creates version N+1 and invalidates any existing approval."
                action={
                  <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                    Cancel
                  </Button>
                }
              />
              <div className="space-y-3 px-5 py-4">
                <Textarea
                  label="Body"
                  value={editBody || (version?.body ?? '')}
                  onChange={(event) => setEditBody(event.target.value)}
                  className="min-h-[180px]"
                />
                <Textarea
                  label="Change note"
                  value={editNote}
                  onChange={(event) => setEditNote(event.target.value)}
                  placeholder="What changed and why"
                  className="min-h-[60px]"
                />
                {edit.error ? <Banner tone="danger">{edit.error.message}</Banner> : null}
                <div className="flex items-center gap-2">
                  <Button variant="primary" loading={edit.pending} onClick={submitEdit}>
                    Save new version
                  </Button>
                  <span className="text-[11px] text-steel-600">
                    This will revoke approval and return the item to Draft.
                  </span>
                </div>
              </div>
            </Card>
          ) : null}

          <Card>
            <CardHeader title="Version history" subtitle="Immutable snapshots, newest first." />
            {versions.loading ? (
              <div className="space-y-2 p-4">
                <Skeleton className="h-10 w-full" />
              </div>
            ) : (versions.data ?? []).length === 0 ? (
              <EmptyState compact title="No versions" description="This item has no versions." />
            ) : (
              <ul className="divide-y divide-white/[0.04]">
                {(versions.data ?? []).map((entry) => (
                  <li key={entry.id} className="px-5 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex items-center gap-2">
                        <Mono>v{entry.version_number}</Mono>
                        {entry.id === data.current_version_id ? (
                          <Badge tone="violet">current</Badge>
                        ) : null}
                        {entry.created_by_agent ? (
                          <AgentChip agent={entry.created_by_agent} size="xs" />
                        ) : null}
                      </div>
                      <span className="text-[10px] text-steel-600">
                        {relativeTime(entry.created_at)}
                      </span>
                    </div>
                    {entry.change_note ? (
                      <p className="mt-1 text-[11px] text-steel-500">{entry.change_note}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          {/* publishing */}
          <Card>
            <CardHeader title="Publishing" subtitle="Gated in Phase 1" />
            <div className="space-y-3 px-5 py-4">
              <div className="flex items-start gap-2 rounded-lg border border-violet-500/20 bg-violet-500/[0.06] px-3 py-2">
                <IconLock size={13} className="mt-0.5 shrink-0 text-violet-300" />
                <p className="text-[11px] leading-relaxed text-violet-200/90">
                  Publishing requires an approval bound to this exact version <em>and</em> a
                  configured platform transport. No transport exists in Phase 1, so publishing is
                  refused rather than simulated.
                </p>
              </div>
              <Button
                variant="primary"
                size="sm"
                className="w-full"
                disabled={!canEdit || !canPublish}
                loading={publish.pending}
                onClick={doPublish}
                title={
                  canPublish
                    ? 'Attempt publish (will be refused without a transport)'
                    : 'Approval required before publishing'
                }
              >
                Attempt publish
              </Button>
              {!canPublish ? (
                <p className="text-[10px] text-steel-600">
                  Disabled: status is {data.status}. Approval is required first.
                </p>
              ) : null}
              {publish.error ? <Banner tone="danger">{publish.error.message}</Banner> : null}
              {publishMessage ? <Banner tone="ok">{publishMessage}</Banner> : null}

              <div className="border-t border-white/[0.05] pt-3">
                <StatRow label="Status" value={data.status} />
                <StatRow
                  label="Approvals"
                  value={`${data.approval_count} event(s)`}
                />
                <StatRow label="Published" value={publications.data?.length ?? 0} />
              </div>
            </div>
          </Card>

          {/* ledger */}
          <Card>
            <CardHeader title="Approval ledger" subtitle="Append-only audit of decisions." />
            {approvals.loading ? (
              <div className="p-4">
                <Skeleton className="h-16 w-full" />
              </div>
            ) : (approvals.data ?? []).length === 0 ? (
              <EmptyState compact title="No events" description="No approvals recorded." />
            ) : (
              <ul className="divide-y divide-white/[0.04]">
                {(approvals.data ?? []).map((record) => (
                  <li key={record.id} className="px-4 py-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] text-steel-300">
                        {record.action.replace('_', ' ')}
                      </span>
                      {record.invalidated_at ? (
                        <Badge tone="danger">invalidated</Badge>
                      ) : record.action === 'approve' ? (
                        <Badge tone="ok">valid</Badge>
                      ) : (
                        <Mono>{record.actor_role}</Mono>
                      )}
                    </div>
                    <p className="mt-0.5 hq-mono">
                      v{record.content_version_id.slice(0, 6)}… ·{' '}
                      {relativeTime(record.created_at)}
                    </p>
                    {record.invalidated_reason ? (
                      <p className="mt-0.5 text-[10px] text-signal-danger/80">
                        {record.invalidated_reason}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {/* accounts */}
          <Card>
            <CardHeader title="Channels" subtitle="Connected accounts for publishing." />
            <div className="px-4 py-3">
              {(accounts.data ?? []).length === 0 ? (
                <p className="text-[11px] text-steel-600">
                  No social accounts registered. Link one in Social Accounts.
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {(accounts.data ?? [])
                    .filter((account) => account.platform === data.platform)
                    .map((account) => (
                      <li
                        key={account.id}
                        className="flex items-center justify-between gap-2 rounded-md border border-white/[0.05] px-2.5 py-1.5"
                      >
                        <PlatformChip platform={account.platform} />
                        <Badge tone={account.status === 'connected' ? 'ok' : 'muted'}>
                          {account.status}
                        </Badge>
                      </li>
                    ))}
                </ul>
              )}
            </div>
          </Card>

          {canEdit && data.status !== 'PUBLISHED' && data.status !== 'CANCELLED' ? (
            <Card>
              <div className="px-4 py-3">
                <SectionTitle>Danger zone</SectionTitle>
                <Button
                  size="sm"
                  variant="danger"
                  className="w-full"
                  loading={cancel.pending}
                  onClick={async () => {
                    const result = await cancel.run(undefined as never)
                    if (result !== null) {
                      notify('Content cancelled.', 'neutral')
                      item.reload()
                    }
                  }}
                >
                  Cancel content item
                </Button>
                {!canApprove ? (
                  <p className="mt-2 text-[10px] text-steel-600">
                    Cancellation is audited. Published items are immutable.
                  </p>
                ) : null}
              </div>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  )
}

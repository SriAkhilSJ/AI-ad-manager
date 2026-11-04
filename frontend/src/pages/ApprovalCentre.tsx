import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { contentApi, newIdempotencyKey } from '../lib/api'
import type { ContentItemDetail } from '../lib/types'
import { relativeTime } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Modal,
  Mono,
  Skeleton,
  StatusBadge,
  Tabs,
  Textarea,
} from '../components/ui'
import { AgentChip, PlatformChip } from '../components/shared'
import { IconApproval, IconLock } from '../components/layout/icons'

type Queue = 'IN_REVIEW' | 'CHANGES_REQUESTED' | 'APPROVED' | 'SCHEDULED'

const QUEUES: Array<{ id: Queue; label: string; hint: string }> = [
  { id: 'IN_REVIEW', label: 'Awaiting review', hint: 'Submitted by the director or a marketer' },
  { id: 'CHANGES_REQUESTED', label: 'Changes requested', hint: 'Needs edits before re-review' },
  { id: 'APPROVED', label: 'Approved', hint: 'Authorised for scheduling' },
  { id: 'SCHEDULED', label: 'Scheduled', hint: 'Queued with a live approval' },
]

export function ApprovalCentrePage() {
  const { canApprove } = useAuth()
  const [queue, setQueue] = useState<Queue>('IN_REVIEW')
  const items = useApi(() => contentApi.list({ status_filter: queue, limit: 100 }), [queue])
  const [selected, setSelected] = useState<string | null>(null)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Governance"
        title="Approval Centre"
        description="Approval binds to an exact content version. Editing the copy or replacing a creative invalidates the approval and returns the item to draft."
      >
        <div className="flex flex-wrap items-center gap-3">
          <Tabs<Queue>
            value={queue}
            onChange={(next) => {
              setQueue(next)
              setSelected(null)
            }}
            tabs={QUEUES.map((entry) => ({ id: entry.id, label: entry.label }))}
          />
          {!canApprove ? (
            <Badge tone="warn" className="ml-auto">
              read-only — founder approval required
            </Badge>
          ) : null}
        </div>
      </PageHeader>

      <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
        <Card>
          <CardHeader
            title={QUEUES.find((entry) => entry.id === queue)?.label}
            subtitle={
              items.data
                ? `${items.data.length} item(s) · ${QUEUES.find((entry) => entry.id === queue)?.hint}`
                : undefined
            }
          />
          {items.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : items.error ? (
            <div className="p-4">
              <ErrorState message={items.error.message} onRetry={items.reload} />
            </div>
          ) : (items.data ?? []).length === 0 ? (
            <EmptyState
              icon={<IconApproval />}
              title="Queue is empty"
              description={
                queue === 'IN_REVIEW'
                  ? 'Nothing is waiting for a decision. Start a Director workflow to generate a review batch.'
                  : 'No items in this state.'
              }
            />
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {(items.data ?? []).map((item) => (
                <li key={item.id}>
                  <button
                    onClick={() => setSelected(item.id)}
                    className={`flex w-full items-center justify-between gap-3 px-5 py-3.5 text-left transition-colors ${
                      selected === item.id ? 'bg-white/[0.05]' : 'hover:bg-white/[0.02]'
                    }`}
                  >
                    <div className="min-w-0">
                      <p className="truncate text-xs font-medium text-steel-300">{item.title}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <PlatformChip platform={item.platform} />
                        {item.created_by_agent ? (
                          <AgentChip agent={item.created_by_agent} size="xs" />
                        ) : null}
                        <Mono>v{item.version_count}</Mono>
                        <span className="text-[10px] text-steel-600">
                          {relativeTime(item.updated_at ?? item.created_at)}
                        </span>
                      </div>
                    </div>
                    <StatusBadge status={item.status} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {selected ? (
          <ReviewPanel
            itemId={selected}
            canApprove={canApprove}
            onChanged={() => items.reload()}
          />
        ) : (
          <Card className="self-start">
            <EmptyState
              compact
              icon={<IconLock />}
              title="Select an item"
              description="Review the exact version that would be published, then record a decision."
            />
          </Card>
        )}
      </div>
    </div>
  )
}

function ReviewPanel({
  itemId,
  canApprove,
  onChanged,
}: {
  itemId: string
  canApprove: boolean
  onChanged: () => void
}) {
  const { notify } = useToast()
  const detail = useApi(() => contentApi.get(itemId), [itemId])
  const approvals = useApi(() => contentApi.approvals(itemId), [itemId])
  const [modalAction, setModalAction] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  const transition = useMutation(
    (payload: { action: string; reason: string }) =>
      contentApi.transition(itemId, payload, newIdempotencyKey(`tr-${payload.action}`)),
  )

  async function submit() {
    if (!modalAction) return
    const result = await transition.run({ action: modalAction, reason })
    if (result) {
      notify(`${modalAction.replace('_', ' ')} recorded. Status: ${result.status}.`, 'ok')
      setModalAction(null)
      setReason('')
      detail.reload()
      approvals.reload()
      onChanged()
    }
  }

  if (detail.loading) {
    return (
      <Card className="self-start">
        <div className="space-y-2 p-4">
          <Skeleton className="h-6 w-1/2" />
          <Skeleton className="h-24 w-full" />
        </div>
      </Card>
    )
  }
  if (detail.error || !detail.data) {
    return (
      <Card className="self-start">
        <div className="p-4">
          <ErrorState message={detail.error?.message ?? 'Not found'} onRetry={detail.reload} />
        </div>
      </Card>
    )
  }

  const item: ContentItemDetail = detail.data
  const version = item.current_version
  const canDo = (action: string) => item.allowed_actions.includes(action)

  return (
    <>
      <Card className="self-start">
        <CardHeader
          title={item.title}
          subtitle={
            <span className="flex flex-wrap items-center gap-2">
              <StatusBadge status={item.status} />
              <Mono>v{item.version_count}</Mono>
              {item.has_valid_approval ? (
                <Badge tone="ok" dot>
                  approval live
                </Badge>
              ) : (
                <Badge tone="danger" dot>
                  no live approval
                </Badge>
              )}
            </span>
          }
        />
        <div className="space-y-4 px-5 py-4">
          <div>
            <p className="hq-label mb-1.5">Copy · version {version?.version_number ?? 1}</p>
            <div className="rounded-lg border border-white/[0.07] bg-ink-900/60 p-3">
              {version?.headline ? (
                <p className="text-sm font-medium text-steel-300">{version.headline}</p>
              ) : null}
              <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-steel-400">
                {version?.body || 'No body copy.'}
              </p>
              {version?.call_to_action ? (
                <p className="mt-2 text-xs text-violet-300">{version.call_to_action}</p>
              ) : null}
              {version?.hashtags?.length ? (
                <p className="mt-2 text-[11px] text-steel-500">{version.hashtags.join(' ')}</p>
              ) : null}
            </div>
            <p className="mt-1.5 hq-mono">hash {version?.content_hash.slice(0, 16)}…</p>
          </div>

          <div className="flex flex-wrap gap-2">
            {canDo('submit') ? (
              <Button size="sm" onClick={() => setModalAction('submit')}>
                Submit for review
              </Button>
            ) : null}
            {canDo('approve') ? (
              <Button
                size="sm"
                variant="success"
                disabled={!canApprove}
                title={canApprove ? undefined : 'Founder only'}
                onClick={() => setModalAction('approve')}
              >
                Approve
              </Button>
            ) : null}
            {canDo('request_changes') ? (
              <Button
                size="sm"
                variant="secondary"
                disabled={!canApprove}
                onClick={() => setModalAction('request_changes')}
              >
                Request changes
              </Button>
            ) : null}
            {canDo('reject') ? (
              <Button
                size="sm"
                variant="danger"
                disabled={!canApprove}
                onClick={() => setModalAction('reject')}
              >
                Reject
              </Button>
            ) : null}
            {canDo('publish') ? (
              <Link to={`/content/${item.id}`}>
                <Button size="sm" variant="ghost">
                  Publish options →
                </Button>
              </Link>
            ) : null}
          </div>

          <div>
            <p className="hq-label mb-1.5">Approval ledger</p>
            {approvals.loading ? (
              <Skeleton className="h-16 w-full" />
            ) : (approvals.data ?? []).length === 0 ? (
              <p className="text-[11px] text-steel-600">No approval events yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {(approvals.data ?? []).slice(0, 6).map((record) => (
                  <li
                    key={record.id}
                    className="flex items-start justify-between gap-2 rounded-md border border-white/[0.05] px-2.5 py-1.5"
                  >
                    <div className="min-w-0">
                      <p className="text-[11px] text-steel-300">
                        {record.action.replace('_', ' ')}
                        <span className="ml-1.5 text-steel-600">by {record.actor_role || 'system'}</span>
                      </p>
                      {record.reason ? (
                        <p className="truncate text-[10px] text-steel-600">{record.reason}</p>
                      ) : null}
                    </div>
                    <div className="shrink-0 text-right">
                      {record.invalidated_at ? (
                        <Badge tone="danger">invalidated</Badge>
                      ) : record.action === 'approve' ? (
                        <Badge tone="ok">valid</Badge>
                      ) : null}
                      <p className="mt-1 hq-mono">{relativeTime(record.created_at)}</p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {transition.error ? <Banner tone="danger">{transition.error.message}</Banner> : null}
        </div>
      </Card>

      <Modal
        open={Boolean(modalAction)}
        onClose={() => setModalAction(null)}
        title={`${(modalAction ?? '').replace('_', ' ')} · ${item.title}`}
        description={
          modalAction === 'approve'
            ? `Approving binds to version ${version?.version_number} (hash ${version?.content_hash.slice(0, 12)}…). Any later edit invalidates it.`
            : 'Every transition is written to the approval ledger and audit log.'
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setModalAction(null)}>
              Cancel
            </Button>
            <Button
              variant={
                modalAction === 'reject'
                  ? 'danger'
                  : modalAction === 'approve'
                    ? 'success'
                    : 'primary'
              }
              loading={transition.pending}
              onClick={submit}
            >
              Confirm
            </Button>
          </>
        }
      >
        <Textarea
          label="Reason (audited)"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Context for this decision…"
        />
      </Modal>
    </>
  )
}

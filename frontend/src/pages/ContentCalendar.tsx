import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { contentApi, socialApi } from '../lib/api'
import type { ContentItem, ContentStatus } from '../lib/types'
import { PLATFORMS } from '../lib/types'
import { formatDateTime, titleCase } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  Skeleton,
  StatusBadge,
  Tabs,
} from '../components/ui'
import { PlatformChip } from '../components/shared'
import { IconCalendar } from '../components/layout/icons'

const STATUS_FILTERS: Array<{ id: ContentStatus | 'ALL'; label: string }> = [
  { id: 'ALL', label: 'All' },
  { id: 'DRAFT', label: 'Draft' },
  { id: 'IN_REVIEW', label: 'In review' },
  { id: 'APPROVED', label: 'Approved' },
  { id: 'SCHEDULED', label: 'Scheduled' },
  { id: 'PUBLISHED', label: 'Published' },
  { id: 'REJECTED', label: 'Rejected' },
  { id: 'CHANGES_REQUESTED', label: 'Changes' },
]

export function ContentCalendarPage() {
  const [status, setStatus] = useState<ContentStatus | 'ALL'>('ALL')
  const [platform, setPlatform] = useState<string>('')
  const content = useApi(
    () =>
      contentApi.list({
        status_filter: status === 'ALL' ? undefined : status,
        platform: platform || undefined,
        limit: 200,
      }),
    [status, platform],
  )
  const scheduled = useApi(() => socialApi.scheduled())

  const grouped = useMemo(() => {
    const map = new Map<string, ContentItem[]>()
    ;(content.data ?? []).forEach((item) => {
      const day = item.created_at.slice(0, 10)
      map.set(day, [...(map.get(day) ?? []), item])
    })
    return Array.from(map.entries()).sort((a, b) => (a[0] < b[0] ? 1 : -1))
  }, [content.data])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Pipeline"
        title="Content Calendar"
        description="Every content item grouped by creation day, with its approval state. Scheduled posts appear separately once an approved item is queued."
      >
        <div className="flex flex-wrap items-center gap-3">
          <Tabs<ContentStatus | 'ALL'>
            value={status}
            onChange={setStatus}
            tabs={STATUS_FILTERS.map((filter) => ({ id: filter.id, label: filter.label }))}
          />
          <div className="ml-auto flex flex-wrap items-center gap-1">
            <button
              onClick={() => setPlatform('')}
              className={`rounded-md px-2 py-1 text-[11px] transition-colors ${
                platform === '' ? 'bg-white/[0.07] text-steel-300' : 'text-steel-600 hover:text-steel-400'
              }`}
            >
              All channels
            </button>
            {PLATFORMS.map((value) => (
              <button
                key={value}
                onClick={() => setPlatform(value)}
                className={`rounded-md px-2 py-1 text-[11px] transition-colors ${
                  platform === value
                    ? 'bg-white/[0.07] text-steel-300'
                    : 'text-steel-600 hover:text-steel-400'
                }`}
              >
                {titleCase(value)}
              </button>
            ))}
          </div>
        </div>
      </PageHeader>

      {content.loading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : content.error ? (
        <ErrorState message={content.error.message} onRetry={content.reload} />
      ) : grouped.length === 0 ? (
        <Card>
          <EmptyState
            icon={<IconCalendar />}
            title="Nothing scheduled or drafted"
            description="Content created by the Director or by hand appears here. Use the Director to generate a first batch."
          />
        </Card>
      ) : (
        <div className="space-y-6">
          {grouped.map(([day, items]) => (
            <section key={day}>
              <div className="mb-2 flex items-center gap-3">
                <p className="text-[11px] font-medium uppercase tracking-brand text-steel-500">
                  {new Date(day).toLocaleDateString(undefined, {
                    weekday: 'short',
                    month: 'short',
                    day: '2-digit',
                  })}
                </p>
                <div className="h-px flex-1 bg-white/[0.05]" />
                <Mono>{items.length}</Mono>
              </div>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {items.map((item) => (
                  <Link key={item.id} to={`/content/${item.id}`}>
                    <Card interactive className="h-full">
                      <div className="flex h-full flex-col gap-2 p-3.5">
                        <div className="flex items-start justify-between gap-2">
                          <p className="line-clamp-2 text-xs font-medium leading-snug text-steel-300">
                            {item.title}
                          </p>
                          <StatusBadge status={item.status} />
                        </div>
                        <div className="mt-auto flex items-center justify-between gap-2 pt-2">
                          <PlatformChip platform={item.platform} />
                          <div className="flex items-center gap-2">
                            {item.created_by_agent ? (
                              <Badge tone="violet">agent</Badge>
                            ) : null}
                            <Mono>v{item.version_count}</Mono>
                          </div>
                        </div>
                      </div>
                    </Card>
                  </Link>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      <Card>
        <CardHeader
          title="Scheduled queue"
          subtitle="Posts with a live approval and a target time. Phase 1 does not dispatch them to a network."
        />
        {scheduled.loading ? (
          <div className="p-4">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : scheduled.error ? (
          <div className="p-4">
            <ErrorState message={scheduled.error.message} onRetry={scheduled.reload} />
          </div>
        ) : (scheduled.data ?? []).length === 0 ? (
          <EmptyState
            compact
            title="Nothing scheduled"
            description="Schedule an approved item from its detail page to place it in this queue."
          />
        ) : (
          <table className="hq-table">
            <thead>
              <tr>
                <th>Content</th>
                <th>Scheduled for</th>
                <th>Status</th>
                <th>Approval</th>
              </tr>
            </thead>
            <tbody>
              {(scheduled.data ?? []).map((post) => (
                <tr key={post.id}>
                  <td>
                    <Link
                      to={`/content/${post.content_item_id}`}
                      className="hq-mono hover:text-violet-300"
                    >
                      {post.content_item_id.slice(0, 8)}…
                    </Link>
                  </td>
                  <td className="text-steel-400">{formatDateTime(post.scheduled_for)}</td>
                  <td>
                    <Badge tone={post.status === 'published' ? 'ok' : 'info'}>{post.status}</Badge>
                  </td>
                  <td>
                    <Mono>{post.approval_record_id.slice(0, 8)}…</Mono>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { campaignApi, contentApi, contentStudioApi, socialApi } from '../lib/api'
import type { CalendarEntry, CalendarLabel, CalendarView, ContentItem, ContentStatus } from '../lib/types'
import { PLATFORMS } from '../lib/types'
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
  Mono,
  Select,
  Skeleton,
  SkeletonRows,
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


const LABEL_TONE: Record<CalendarLabel, 'ok' | 'warn' | 'danger' | 'neutral' | 'violet' | 'info' | 'muted'> = {
  planned: 'info',
  draft: 'muted',
  in_review: 'warn',
  approved: 'ok',
  changes_requested: 'warn',
  rejected: 'danger',
  scheduled_internally: 'info',
  cancelled: 'muted',
  failed: 'danger',
}

const LABEL_TEXT: Record<CalendarLabel, string> = {
  planned: 'Planned',
  draft: 'Draft',
  in_review: 'In review',
  approved: 'Approved',
  changes_requested: 'Changes requested',
  rejected: 'Rejected',
  scheduled_internally: 'Planned (internal)',
  cancelled: 'Cancelled',
  failed: 'Failed',
}

function shiftAnchor(anchor: string, view: CalendarView['view'], direction: number) {
  const date = new Date(`${anchor}T00:00:00`)
  if (view === 'week') date.setDate(date.getDate() + 7 * direction)
  else if (view === 'month') date.setMonth(date.getMonth() + direction)
  else date.setDate(date.getDate() + 14 * direction)
  return date.toISOString().slice(0, 10)
}

function PlannerEntry({
  entry,
  onMoved,
}: {
  entry: CalendarEntry
  onMoved: () => void
}) {
  const [date, setDate] = useState(entry.planned_date)
  const [time, setTime] = useState(entry.planned_time)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dirty = date !== entry.planned_date || time !== entry.planned_time

  const move = async () => {
    setPending(true)
    setError(null)
    try {
      await contentStudioApi.moveCalendar(entry.content_item_id, {
        planned_date: date,
        planned_time: time,
        note: 'Moved in the internal calendar',
      })
      onMoved()
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="rounded-lg border border-white/[0.06] p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Link
            to={`/content-studio/items/${entry.content_item_id}`}
            className="line-clamp-2 text-xs font-medium leading-snug text-steel-300 hover:text-violet-200"
          >
            {entry.title}
          </Link>
          <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-steel-600">
            <PlatformChip platform={entry.platform} />
            <span>{titleCase(entry.content_type)}</span>
            {entry.campaign_id ? <span>{entry.campaign_id.slice(0, 8)}</span> : null}
          </p>
        </div>
        <Badge tone={LABEL_TONE[entry.label]}>{LABEL_TEXT[entry.label]}</Badge>
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Input
          label="Date"
          type="date"
          value={date}
          onChange={(event) => setDate(event.target.value)}
        />
        <Input
          label="Time"
          type="time"
          value={time}
          onChange={(event) => setTime(event.target.value)}
        />
        <Button onClick={move} disabled={!dirty || pending}>
          {pending ? 'Moving…' : 'Move'}
        </Button>
      </div>
      {entry.note ? <p className="mt-1 text-[11px] text-steel-600">{entry.note}</p> : null}
      {error ? <p className="mt-1 text-[11px] text-signal-danger">{error}</p> : null}
    </div>
  )
}

function InternalPlannerSection() {
  const [view, setView] = useState<CalendarView['view']>('month')
  const [anchor, setAnchor] = useState(() => new Date().toISOString().slice(0, 10))
  const [platform, setPlatform] = useState('')
  const [campaignId, setCampaignId] = useState('')
  const campaigns = useApi(() => campaignApi.list())
  const calendar = useApi(
    () =>
      contentStudioApi.calendar({
        view,
        anchor,
        platform: platform || undefined,
        campaign_id: campaignId || undefined,
      }),
    [view, anchor, platform, campaignId],
  )

  const grouped = useMemo(() => {
    const map = new Map<string, CalendarEntry[]>()
    ;(calendar.data?.entries ?? []).forEach((entry) => {
      map.set(entry.planned_date, [...(map.get(entry.planned_date) ?? []), entry])
    })
    return Array.from(map.entries()).sort((a, b) => (a[0] < b[0] ? -1 : 1))
  }, [calendar.data])

  return (
    <Card>
      <CardHeader
        title="Internal content calendar"
        subtitle={
          calendar.data
            ? `${calendar.data.entries.length} planned drafts · ${calendar.data.start} → ${calendar.data.end}`
            : 'Planned publishing times inside this app'
        }
        action={
          <Tabs<CalendarView['view']>
            tabs={[
              { id: 'month', label: 'Month' },
              { id: 'week', label: 'Week' },
              { id: 'list', label: 'List' },
            ]}
            value={view}
            onChange={setView}
          />
        }
      />

      <Banner tone="neutral" title="Internal planning only">
        These dates and times are a plan for the team. Nothing is scheduled on Instagram, LinkedIn,
        YouTube, X, Facebook or Reddit, and no publishing API is called.
      </Banner>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={() => setAnchor(shiftAnchor(anchor, view, -1))}>
          ← Previous
        </Button>
        <Input
          label="Anchor"
          type="date"
          value={anchor}
          onChange={(event) => setAnchor(event.target.value)}
        />
        <Button variant="ghost" onClick={() => setAnchor(shiftAnchor(anchor, view, 1))}>
          Next →
        </Button>
        <Select label="Platform" value={platform} onChange={(event) => setPlatform(event.target.value)}>
          <option value="">All platforms</option>
          {PLATFORMS.map((value) => (
            <option key={value} value={value}>
              {titleCase(value)}
            </option>
          ))}
        </Select>
        <Select
          label="Campaign"
          value={campaignId}
          onChange={(event) => setCampaignId(event.target.value)}
        >
          <option value="">All campaigns</option>
          {(campaigns.data ?? []).map((campaign) => (
            <option key={campaign.id} value={campaign.id}>
              {campaign.name}
            </option>
          ))}
        </Select>
      </div>

      <div className="mt-4">
        {calendar.loading ? (
          <SkeletonRows rows={4} />
        ) : calendar.error ? (
          <ErrorState message={calendar.error.message} onRetry={calendar.reload} />
        ) : grouped.length === 0 ? (
          <EmptyState
            title="Nothing planned in this window"
            description="Drafts generated by the Content Studio carry a suggested date and appear here."
          />
        ) : (
          <div className="space-y-4">
            {grouped.map(([day, entries]) => (
              <section key={day}>
                <div className="mb-2 flex items-center gap-3">
                  <p className="text-[11px] font-medium uppercase tracking-brand text-steel-500">
                    {new Date(`${day}T00:00:00`).toLocaleDateString(undefined, {
                      weekday: 'short',
                      month: 'short',
                      day: '2-digit',
                    })}
                  </p>
                  <div className="h-px flex-1 bg-white/[0.05]" />
                  <Mono>{entries.length}</Mono>
                </div>
                <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                  {entries.map((entry) => (
                    <PlannerEntry
                      key={entry.entry_id}
                      entry={entry}
                      onMoved={() => calendar.reload()}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </Card>
  )
}

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
        description="Internal planning calendar for content drafts, plus every content item grouped by creation day with its approval state. Nothing here is scheduled on a real social platform."
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

      <InternalPlannerSection />

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

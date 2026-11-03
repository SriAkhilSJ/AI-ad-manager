import clsx from 'clsx'
import type { ReactNode } from 'react'
import type { ApprovalFinding, PlanTask, PlatformStatus } from '../lib/types'
import { platformLabel, titleCase } from '../lib/format'
import { Badge, Card, CardHeader, EmptyState, Mono } from './ui'

// ------------------------------------------------------------------ Platform
export function PlatformChip({ platform }: { platform: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-white/[0.09] bg-white/[0.03] px-2 py-0.5 text-[11px] text-steel-400">
      <PlatformMark platform={platform} />
      {platformLabel(platform)}
    </span>
  )
}

export function PlatformMark({ platform }: { platform: string }) {
  const glyph: Record<string, string> = {
    instagram: '◍',
    linkedin: 'in',
    youtube: '▶',
    x: '✕',
    facebook: 'f',
    reddit: '◉',
  }
  const color: Record<string, string> = {
    instagram: 'text-[#C13584]',
    linkedin: 'text-[#4A9CD6]',
    youtube: 'text-[#FF4D4D]',
    x: 'text-steel-300',
    facebook: 'text-[#4A7BD6]',
    reddit: 'text-[#FF7A45]',
  }
  return (
    <span
      className={clsx(
        'inline-flex h-4 w-4 items-center justify-center rounded-[4px] bg-white/[0.05] text-[9px] font-semibold',
        color[platform] ?? 'text-steel-400',
      )}
    >
      {glyph[platform] ?? platform.slice(0, 1).toUpperCase()}
    </span>
  )
}

// ------------------------------------------------------------------- Agents
export const AGENT_LABEL: Record<string, string> = {
  research: 'Research',
  strategy: 'Strategy',
  content: 'Content',
  creative: 'Creative',
  brand_guardian: 'Brand Guardian',
  growth_analyst: 'Growth Analyst',
}

export function AgentChip({ agent, size = 'sm' }: { agent: string; size?: 'sm' | 'xs' }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-md border border-violet-500/25 bg-violet-500/[0.08] font-medium text-violet-300',
        size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-1.5 py-0.5 text-[10px]',
      )}
    >
      {AGENT_LABEL[agent] ?? titleCase(agent)}
    </span>
  )
}

// ------------------------------------------------------------- PlanView
const TASK_STATUS_TONE = {
  done: 'ok',
  running: 'info',
  pending: 'muted',
  failed: 'danger',
  skipped: 'muted',
} as const

export function PlanView({ plan }: { plan: PlanTask[] | null }) {
  if (!plan || plan.length === 0) {
    return (
      <EmptyState
        compact
        title="No plan yet"
        description="The director builds a dependency-ordered task graph once a workflow starts."
      />
    )
  }
  const done = plan.filter((task) => task.status === 'done').length
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-[11px] text-steel-500">
        <span>
          {done}/{plan.length} tasks complete
        </span>
        <span className="hq-mono">{Math.round((done / plan.length) * 100)}%</span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className="h-full rounded-full bg-gradient-to-r from-violet-600 to-violet-400 transition-all duration-500"
          style={{ width: `${(done / plan.length) * 100}%` }}
        />
      </div>
      <ul className="divide-y divide-white/[0.04]">
        {plan.map((task) => (
          <li key={task.id} className="flex items-start justify-between gap-3 py-2.5">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <AgentChip agent={task.agent} size="xs" />
                <span className="text-xs text-steel-400">{titleCase(task.action)}</span>
              </div>
              <p className="mt-1 hq-mono">
                {task.id}
                {task.depends_on.length ? ` · after ${task.depends_on.join(', ')}` : ' · root'}
              </p>
            </div>
            <Badge tone={TASK_STATUS_TONE[task.status] ?? 'muted'}>{task.status}</Badge>
          </li>
        ))}
      </ul>
    </div>
  )
}

// --------------------------------------------------------- QA findings
export function QAFindings({ findings }: { findings: ApprovalFinding[] | null | undefined }) {
  if (!findings || findings.length === 0) {
    return (
      <EmptyState
        compact
        title="No QA findings"
        description="The Brand Guardian reports violations here after drafts are generated."
      />
    )
  }
  return (
    <div className="space-y-3">
      {findings.map((finding, index) => (
        <div key={index} className="rounded-lg border border-white/[0.07] bg-white/[0.02] p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {finding.platform ? <PlatformChip platform={finding.platform} /> : null}
              <Badge
                tone={
                  finding.verdict === 'pass'
                    ? 'ok'
                    : finding.verdict === 'blocked'
                      ? 'danger'
                      : 'warn'
                }
              >
                {finding.verdict ?? 'unknown'}
              </Badge>
            </div>
            {finding.score !== undefined ? (
              <Mono>score {finding.score.toFixed(2)}</Mono>
            ) : null}
          </div>
          {finding.error ? (
            <p className="mt-2 text-[11px] text-signal-danger">{finding.error}</p>
          ) : null}
          {finding.violations && finding.violations.length > 0 ? (
            <ul className="mt-2.5 space-y-1.5">
              {finding.violations.map((violation, vIndex) => (
                <li key={vIndex} className="flex items-start gap-2 text-[11px] leading-relaxed">
                  <span
                    className={clsx(
                      'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                      violation.severity === 'blocking' ? 'bg-signal-danger' : 'bg-signal-warn',
                    )}
                  />
                  <span className="text-steel-400">
                    <span className="text-steel-300">{violation.message}</span>
                    {violation.fix ? (
                      <span className="block text-steel-600">Fix: {violation.fix}</span>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ))}
    </div>
  )
}

// --------------------------------------------------------------- Integrity
export function IntegrityBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <Badge tone={ok ? 'ok' : 'danger'} dot>
      {label}
    </Badge>
  )
}

// ------------------------------------------------------------ Integration
export function IntegrationRow({
  name,
  status,
  note,
  configured,
}: {
  name: string
  status: string
  note?: string | null
  configured?: boolean
}) {
  const tone =
    status === 'available' ? 'ok' : status === 'degraded' ? 'warn' : 'muted'
  return (
    <div className="flex items-start justify-between gap-4 border-b border-white/[0.04] py-2.5 last:border-0">
      <div className="min-w-0">
        <p className="text-xs text-steel-300">{name}</p>
        {note ? <p className="mt-0.5 text-[11px] leading-relaxed text-steel-600">{note}</p> : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {configured === false ? <Mono>unconfigured</Mono> : null}
        <Badge tone={tone} dot>
          {status}
        </Badge>
      </div>
    </div>
  )
}

// ------------------------------------------------------------- Sparkline
export function Sparkline({
  points,
  className,
}: {
  points: number[]
  className?: string
}) {
  if (points.length < 2) return null
  const max = Math.max(...points, 1)
  const min = Math.min(...points, 0)
  const range = max - min || 1
  const coords = points.map((value, index) => {
    const x = (index / (points.length - 1)) * 100
    const y = 100 - ((value - min) / range) * 100
    return `${x.toFixed(2)},${y.toFixed(2)}`
  })
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className={clsx('h-16 w-full', className)}>
      <defs>
        <linearGradient id="spark" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#8B5CF6" stopOpacity="0.35" />
          <stop offset="100%" stopColor="#8B5CF6" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polyline
        points={`0,100 ${coords.join(' ')} 100,100`}
        fill="url(#spark)"
        stroke="none"
      />
      <polyline
        points={coords.join(' ')}
        fill="none"
        stroke="#A78BFA"
        strokeWidth="1.5"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}

// ---------------------------------------------------------- ContentCard
export function StatRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="text-[11px] text-steel-600">{label}</span>
      <span className="text-xs tabular-nums text-steel-300">{value}</span>
    </div>
  )
}

export function Panel({
  title,
  subtitle,
  action,
  children,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <Card className={className}>
      <CardHeader title={title} subtitle={subtitle} action={action} />
      <div className="px-5 py-4">{children}</div>
    </Card>
  )
}

export function PlatformAvailability({ platforms }: { platforms: PlatformStatus[] }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {platforms.map((row) => (
        <div
          key={row.platform}
          className="flex items-start justify-between gap-3 rounded-lg border border-white/[0.07] bg-white/[0.02] px-3 py-2.5"
        >
          <div className="min-w-0">
            <PlatformChip platform={row.platform} />
            <p className="mt-1.5 text-[10px] leading-relaxed text-steel-600">{row.reason}</p>
          </div>
          <Badge tone={row.publish_enabled ? 'ok' : 'muted'}>
            {row.publish_enabled ? 'ready' : 'gated'}
          </Badge>
        </div>
      ))}
    </div>
  )
}

/**
 * Design system primitives.
 *
 * Dark premium surfaces, metallic silver text, subtle purple accents. Everything
 * is presentational and dependency-free apart from `clsx`.
 */

import clsx from 'clsx'
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from 'react'
import { useEffect } from 'react'
import type { ContentStatus } from '../../lib/types'
import { STATUS_LABEL } from '../../lib/format'

// --------------------------------------------------------------------- Card
export function Card({
  children,
  className,
  interactive,
  as: Tag = 'div',
}: {
  children: ReactNode
  className?: string
  interactive?: boolean
  as?: 'div' | 'section' | 'article' | 'li'
}) {
  return (
    <Tag className={clsx('hq-panel', interactive && 'hq-panel-hover', className)}>{children}</Tag>
  )
}

export function CardHeader({
  title,
  subtitle,
  action,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      className={clsx(
        'flex items-start justify-between gap-4 border-b border-white/[0.06] px-5 py-4',
        className,
      )}
    >
      <div className="min-w-0">
        <h3 className="text-sm font-medium tracking-tight text-steel-300">{title}</h3>
        {subtitle ? (
          <p className="mt-1 text-xs leading-relaxed text-steel-500">{subtitle}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

// ------------------------------------------------------------------- Button
type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success'
type ButtonSize = 'sm' | 'md'

export function Button({
  children,
  variant = 'secondary',
  size = 'md',
  loading,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}) {
  const variants: Record<ButtonVariant, string> = {
    primary:
      'bg-violet-600 text-white hover:bg-violet-500 border border-violet-500/40 shadow-glow disabled:bg-violet-700/50',
    secondary:
      'bg-white/[0.04] text-steel-300 hover:bg-white/[0.08] border border-white/[0.09] hover:border-white/[0.16]',
    ghost: 'bg-transparent text-steel-400 hover:text-steel-300 hover:bg-white/[0.05] border border-transparent',
    danger:
      'bg-signal-danger/10 text-signal-danger hover:bg-signal-danger/20 border border-signal-danger/30',
    success:
      'bg-signal-ok/10 text-signal-ok hover:bg-signal-ok/20 border border-signal-ok/30',
  }
  const sizes: Record<ButtonSize, string> = {
    sm: 'h-8 px-3 text-xs',
    md: 'h-9 px-4 text-[13px]',
  }
  return (
    <button
      className={clsx(
        'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-all duration-150',
        'disabled:cursor-not-allowed disabled:opacity-50',
        variants[variant],
        sizes[size],
        className,
      )}
      disabled={props.disabled || loading}
      {...props}
    >
      {loading ? <Spinner size={14} /> : null}
      {children}
    </button>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-block animate-spin rounded-full border-2 border-current border-t-transparent align-[-2px]"
      style={{ width: size, height: size }}
    />
  )
}

// -------------------------------------------------------------------- Badge
type BadgeTone = 'neutral' | 'violet' | 'ok' | 'warn' | 'danger' | 'info' | 'muted'

const TONES: Record<BadgeTone, string> = {
  neutral: 'border-white/[0.12] bg-white/[0.05] text-steel-300',
  violet: 'border-violet-500/30 bg-violet-500/10 text-violet-300',
  ok: 'border-signal-ok/30 bg-signal-ok/10 text-signal-ok',
  warn: 'border-signal-warn/30 bg-signal-warn/10 text-signal-warn',
  danger: 'border-signal-danger/30 bg-signal-danger/10 text-signal-danger',
  info: 'border-signal-info/30 bg-signal-info/10 text-signal-info',
  muted: 'border-white/[0.07] bg-transparent text-steel-500',
}

export function Badge({
  children,
  tone = 'neutral',
  className,
  dot,
}: {
  children: ReactNode
  tone?: BadgeTone
  className?: string
  dot?: boolean
}) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-5',
        TONES[tone],
        className,
      )}
    >
      {dot ? <span className="h-1.5 w-1.5 rounded-full bg-current" /> : null}
      {children}
    </span>
  )
}

const STATUS_TONE: Record<string, BadgeTone> = {
  DRAFT: 'muted',
  IN_REVIEW: 'warn',
  APPROVED: 'ok',
  SCHEDULED: 'info',
  PUBLISHING: 'info',
  PUBLISHED: 'violet',
  REJECTED: 'danger',
  CHANGES_REQUESTED: 'warn',
  FAILED: 'danger',
  CANCELLED: 'muted',
}

export function StatusBadge({ status }: { status: ContentStatus | string }) {
  const tone = STATUS_TONE[status] ?? 'neutral'
  const label = STATUS_LABEL[status as ContentStatus] ?? status
  return (
    <Badge tone={tone} dot>
      {label}
    </Badge>
  )
}

export function VerdictBadge({ verdict }: { verdict?: string | null }) {
  if (!verdict) return <Badge tone="muted">Not run</Badge>
  const tone: BadgeTone =
    verdict === 'pass' ? 'ok' : verdict === 'blocked' ? 'danger' : verdict === 'no_content' ? 'muted' : 'warn'
  const label =
    verdict === 'pass'
      ? 'Passed QA'
      : verdict === 'blocked'
        ? 'Blocked'
        : verdict === 'needs_changes'
          ? 'Needs changes'
          : verdict === 'no_content'
            ? 'No content'
            : verdict
  return <Badge tone={tone} dot>{label}</Badge>
}

// -------------------------------------------------------------------- Input
export function Input({
  label,
  hint,
  error,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label?: string; hint?: string; error?: string }) {
  return (
    <label className="block">
      {label ? <span className="hq-label mb-1.5 block">{label}</span> : null}
      <input className={clsx('hq-input', error && 'border-signal-danger/50', className)} {...props} />
      {hint ? <span className="mt-1 block text-[11px] text-steel-600">{hint}</span> : null}
      {error ? <span className="mt-1 block text-[11px] text-signal-danger">{error}</span> : null}
    </label>
  )
}

export function Textarea({
  label,
  hint,
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string; hint?: string }) {
  return (
    <label className="block">
      {label ? <span className="hq-label mb-1.5 block">{label}</span> : null}
      <textarea className={clsx('hq-input min-h-[96px] resize-y', className)} {...props} />
      {hint ? <span className="mt-1 block text-[11px] text-steel-600">{hint}</span> : null}
    </label>
  )
}

export function Select({
  label,
  children,
  className,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement> & { label?: string }) {
  return (
    <label className="block">
      {label ? <span className="hq-label mb-1.5 block">{label}</span> : null}
      <div className="relative">
        <select className={clsx('hq-select', className)} {...props}>
          {children}
        </select>
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-steel-600">
          ▾
        </span>
      </div>
    </label>
  )
}

// --------------------------------------------------------------- EmptyState
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  compact,
}: {
  icon?: ReactNode
  title: string
  description?: ReactNode
  action?: ReactNode
  className?: string
  compact?: boolean
}) {
  return (
    <div
      className={clsx(
        'flex flex-col items-center justify-center text-center',
        compact ? 'px-4 py-8' : 'px-6 py-16',
        className,
      )}
    >
      {icon ? (
        <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.02] text-steel-500">
          {icon}
        </div>
      ) : null}
      <p className="text-sm font-medium text-steel-300">{title}</p>
      {description ? (
        <p className="mt-1.5 max-w-md text-xs leading-relaxed text-steel-500">{description}</p>
      ) : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  )
}

// ----------------------------------------------------------------- Skeleton
export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx('hq-skeleton', className)} />
}

export function SkeletonRows({ rows = 4, height = 'h-12' }: { rows?: number; height?: string }) {
  return (
    <div className="space-y-2 p-4">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className={clsx('w-full', height)} />
      ))}
    </div>
  )
}

// ------------------------------------------------------------------ Banner
export function Banner({
  tone = 'info',
  title,
  children,
  action,
}: {
  tone?: 'info' | 'warn' | 'danger' | 'ok' | 'neutral'
  title?: ReactNode
  children?: ReactNode
  action?: ReactNode
}) {
  const styles = {
    info: 'border-signal-info/25 bg-signal-info/[0.06] text-signal-info',
    warn: 'border-signal-warn/25 bg-signal-warn/[0.06] text-signal-warn',
    danger: 'border-signal-danger/25 bg-signal-danger/[0.06] text-signal-danger',
    ok: 'border-signal-ok/25 bg-signal-ok/[0.06] text-signal-ok',
    neutral: 'border-white/[0.08] bg-white/[0.02] text-steel-400',
  }[tone]
  return (
    <div className={clsx('flex items-start justify-between gap-4 rounded-lg border px-4 py-3', styles)}>
      <div className="min-w-0">
        {title ? <p className="text-xs font-semibold">{title}</p> : null}
        {children ? (
          <div className={clsx('text-xs leading-relaxed', title && 'mt-1 opacity-80')}>{children}</div>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

// -------------------------------------------------------------------- Modal
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  width = 'max-w-lg',
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children?: ReactNode
  footer?: ReactNode
  width?: string
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-6 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        className={clsx(
          'mt-[6vh] w-full animate-fade-in rounded-xl border border-white/[0.1] bg-ink-850 shadow-2xl',
          width,
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-white/[0.07] px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold tracking-tight text-steel-300">{title}</h2>
            {description ? (
              <p className="mt-1 text-xs leading-relaxed text-steel-500">{description}</p>
            ) : null}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-md px-1.5 py-0.5 text-steel-500 transition-colors hover:bg-white/[0.06] hover:text-steel-300"
          >
            ✕
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-white/[0.07] px-5 py-3.5">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------- Toast
export function Toast({
  tone = 'neutral',
  message,
  onDismiss,
}: {
  tone?: 'neutral' | 'ok' | 'danger'
  message: ReactNode
  onDismiss: () => void
}) {
  const border = {
    neutral: 'border-white/[0.12]',
    ok: 'border-signal-ok/40',
    danger: 'border-signal-danger/40',
  }[tone]
  return (
    <div
      className={clsx(
        'pointer-events-auto flex max-w-md animate-fade-in items-start gap-3 rounded-lg border bg-ink-800 px-4 py-3 text-xs text-steel-300 shadow-2xl',
        border,
      )}
    >
      <span className="flex-1 leading-relaxed">{message}</span>
      <button
        onClick={onDismiss}
        aria-label="Dismiss"
        className="text-steel-500 transition-colors hover:text-steel-300"
      >
        ✕
      </button>
    </div>
  )
}

// -------------------------------------------------------------------- Tabs
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  className,
}: {
  tabs: Array<{ id: T; label: string; count?: number }>
  value: T
  onChange: (id: T) => void
  className?: string
}) {
  return (
    <div className={clsx('flex items-center gap-1 overflow-x-auto no-scrollbar', className)}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => onChange(tab.id)}
          className={clsx(
            'relative whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
            value === tab.id
              ? 'bg-white/[0.07] text-steel-300'
              : 'text-steel-500 hover:bg-white/[0.04] hover:text-steel-400',
          )}
        >
          {tab.label}
          {tab.count !== undefined ? (
            <span className="ml-1.5 text-[10px] text-steel-600">{tab.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  )
}

// -------------------------------------------------------------- MetricCard
export function MetricCard({
  label,
  value,
  hint,
  tone = 'neutral',
  unavailable,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'neutral' | 'violet' | 'ok' | 'warn'
  unavailable?: boolean
}) {
  const accent = {
    neutral: 'text-steel-300',
    violet: 'text-violet-300',
    ok: 'text-signal-ok',
    warn: 'text-signal-warn',
  }[tone]
  return (
    <div className="hq-panel px-4 py-3.5">
      <p className="hq-label">{label}</p>
      <p
        className={clsx(
          'mt-2 text-xl font-semibold tracking-tight tabular-nums',
          unavailable ? 'text-steel-600' : accent,
        )}
      >
        {value}
      </p>
      {hint ? <p className="mt-1 text-[11px] leading-relaxed text-steel-600">{hint}</p> : null}
    </div>
  )
}

// ------------------------------------------------------------------ KeyVal
export function KeyValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <span className="text-[11px] uppercase tracking-brand text-steel-600">{label}</span>
      <span className="text-right text-xs text-steel-300">{children}</span>
    </div>
  )
}

// -------------------------------------------------------------------- Misc
export function SectionTitle({
  children,
  action,
}: {
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <h2 className="text-[11px] font-medium uppercase tracking-brand text-steel-500">{children}</h2>
      {action}
    </div>
  )
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx('hq-mono', className)}>{children}</span>
}

export function Divider({ className }: { className?: string }) {
  return <div className={clsx('hq-divider my-4', className)} />
}

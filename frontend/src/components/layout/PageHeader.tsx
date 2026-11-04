import type { ReactNode } from 'react'

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  children,
}: {
  eyebrow?: string
  title: string
  description?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="mb-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          {eyebrow ? (
            <p className="text-[10px] font-medium uppercase tracking-brand text-violet-400/80">
              {eyebrow}
            </p>
          ) : null}
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-steel-300">{title}</h1>
          {description ? (
            <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-steel-500">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {children ? <div className="mt-4">{children}</div> : null}
    </div>
  )
}

export function ErrorState({
  title = 'Could not load data',
  message,
  onRetry,
}: {
  title?: string
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="rounded-lg border border-signal-danger/25 bg-signal-danger/[0.06] px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold text-signal-danger">{title}</p>
          <p className="mt-1 text-xs leading-relaxed text-steel-400">{message}</p>
        </div>
        {onRetry ? (
          <button
            onClick={onRetry}
            className="shrink-0 rounded-md border border-white/[0.12] px-2.5 py-1 text-[11px] text-steel-300 transition-colors hover:bg-white/[0.06]"
          >
            Retry
          </button>
        ) : null}
      </div>
    </div>
  )
}

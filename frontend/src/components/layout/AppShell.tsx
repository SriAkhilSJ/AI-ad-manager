import { NavLink, Outlet, useLocation } from 'react-router-dom'
import clsx from 'clsx'
import { useAuth } from '../../hooks/useAuth'
import { useApi } from '../../hooks/useApi'
import { systemApi } from '../../lib/api'
import { Badge, Spinner } from '../ui'
import {
  IconAnalytics,
  IconApproval,
  IconBrand,
  IconCalendar,
  IconCampaign,
  IconDirector,
  IconOverview,
  IconSettings,
  IconSocial,
  IconStudio,
} from './icons'

export interface NavItem {
  to: string
  label: string
  icon: (props: { size?: number; className?: string }) => JSX.Element
  end?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Overview', icon: IconOverview, end: true },
  { to: '/director', label: 'AI Marketing Director', icon: IconDirector },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaign },
  { to: '/calendar', label: 'Content Calendar', icon: IconCalendar },
  { to: '/creative', label: 'Creative Studio', icon: IconStudio },
  { to: '/approvals', label: 'Approval Centre', icon: IconApproval },
  { to: '/social', label: 'Social Accounts', icon: IconSocial },
  { to: '/analytics', label: 'Analytics', icon: IconAnalytics },
  { to: '/brand', label: 'Brand Knowledge', icon: IconBrand },
  { to: '/settings', label: 'Settings', icon: IconSettings },
]

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.12] bg-gradient-to-br from-white/[0.12] to-transparent">
        <span className="text-[13px] font-semibold tracking-tight text-steel-300">C</span>
        <span className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full bg-violet-500" />
      </div>
      <div className="leading-tight">
        <p className="text-[13px] font-semibold tracking-tight text-steel-300">CRUSOR</p>
        <p className="text-[9px] uppercase tracking-brand text-steel-600">Marketing HQ</p>
      </div>
    </div>
  )
}

export function AppShell() {
  const { user, logout, role } = useAuth()
  const location = useLocation()
  const status = useApi(() => systemApi.status(), [], { enabled: Boolean(user) })

  const current = NAV_ITEMS.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
  )

  return (
    <div className="flex h-full min-h-screen">
      {/* ------------------------------------------------------- sidebar */}
      <aside className="hidden w-[248px] shrink-0 flex-col border-r border-white/[0.06] bg-ink-950/60 lg:flex">
        <div className="flex h-16 items-center border-b border-white/[0.06] px-5">
          <BrandMark />
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-4">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  clsx(
                    'group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] transition-colors',
                    isActive
                      ? 'bg-white/[0.06] text-steel-300'
                      : 'text-steel-500 hover:bg-white/[0.03] hover:text-steel-400',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <span
                      className={clsx(
                        'flex h-6 w-6 items-center justify-center rounded-md transition-colors',
                        isActive
                          ? 'text-violet-300'
                          : 'text-steel-600 group-hover:text-steel-400',
                      )}
                    >
                      <Icon size={15} />
                    </span>
                    <span className="truncate">{item.label}</span>
                    {isActive ? (
                      <span className="ml-auto h-1 w-1 rounded-full bg-violet-400" />
                    ) : null}
                  </>
                )}
              </NavLink>
            )
          })}
        </nav>

        <div className="border-t border-white/[0.06] px-4 py-3">
          <div className="flex items-center gap-2 text-[10px] text-steel-600">
            <span className="h-1.5 w-1.5 rounded-full bg-signal-ok/70" />
            <span>Approval required mode</span>
          </div>
          <div className="mt-2 flex items-center justify-between text-[10px] text-steel-600">
            <span className="hq-mono">
              {status.data ? `v${status.data.app.version}` : status.loading ? '…' : 'offline'}
            </span>
            <span className="hq-mono">{role ?? '—'}</span>
          </div>
        </div>
      </aside>

      {/* ---------------------------------------------------------- main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-4 border-b border-white/[0.06] bg-ink-900/85 px-6 backdrop-blur">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="truncate text-sm font-semibold tracking-tight text-steel-300">
                {current?.label ?? 'CRUSOR MARKETING HQ'}
              </h1>
            </div>
            {status.data ? (
              <p className="mt-0.5 text-[11px] text-steel-600">
                {status.data.app.environment} · {status.data.database.dialect} · director{' '}
                {status.data.director.orchestrator}
              </p>
            ) : status.loading ? (
              <p className="mt-0.5 flex items-center gap-1.5 text-[11px] text-steel-600">
                <Spinner size={10} /> checking platform status…
              </p>
            ) : (
              <p className="mt-0.5 text-[11px] text-signal-danger/80">status unavailable</p>
            )}
          </div>

          <div className="flex items-center gap-3">
            {status.data?.publishing.status === 'unavailable' ? (
              <Badge tone="violet">Publishing gated</Badge>
            ) : null}
            <div className="hidden text-right sm:block">
              <p className="text-[12px] text-steel-300">{user?.full_name || user?.email}</p>
              <p className="text-[10px] uppercase tracking-brand text-steel-600">{user?.role}</p>
            </div>
            <button
              onClick={logout}
              className="rounded-lg border border-white/[0.08] px-3 py-1.5 text-[11px] text-steel-400 transition-colors hover:border-white/[0.16] hover:text-steel-300"
            >
              Sign out
            </button>
          </div>
        </header>

        <main className="min-w-0 flex-1 px-6 py-6">
          <div className="mx-auto w-full max-w-[1400px] animate-fade-in">
            <Outlet />
          </div>
        </main>

        {/* mobile nav */}
        <nav className="sticky bottom-0 z-30 flex items-center gap-1 overflow-x-auto border-t border-white/[0.06] bg-ink-950/90 px-2 py-2 no-scrollbar lg:hidden">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  clsx(
                    'flex min-w-[56px] flex-col items-center gap-1 rounded-md px-2 py-1 text-[9px]',
                    isActive ? 'text-violet-300' : 'text-steel-600',
                  )
                }
              >
                <Icon size={16} />
                <span className="max-w-[64px] truncate">{item.label.split(' ')[0]}</span>
              </NavLink>
            )
          })}
        </nav>
      </div>
    </div>
  )
}

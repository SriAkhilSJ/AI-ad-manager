import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from './hooks/useAuth'
import { AppShell } from './components/layout/AppShell'
import { Spinner } from './components/ui'
import { LoginPage } from './pages/Login'
import { OverviewPage } from './pages/Overview'
import { DirectorPage } from './pages/Director'
import { ResearchCentrePage } from './pages/ResearchCentre'
import { StrategyStudioPage } from './pages/StrategyStudio'
import { StrategyDirectorPage } from './pages/StrategyDirector'
import { CampaignsPage } from './pages/Campaigns'
import { CampaignDetailPage } from './pages/CampaignDetail'
import { ContentCalendarPage } from './pages/ContentCalendar'
import { ContentDetailPage } from './pages/ContentDetail'
import { CreativeStudioPage } from './pages/CreativeStudio'
import { ApprovalCentrePage } from './pages/ApprovalCentre'
import { SocialAccountsPage } from './pages/SocialAccounts'
import { AnalyticsPage } from './pages/Analytics'
import { BrandKnowledgePage } from './pages/BrandKnowledge'
import { SettingsPage } from './pages/Settings'

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="flex items-center gap-2 text-xs text-steel-500">
          <Spinner size={14} /> restoring session…
        </div>
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  return <>{children}</>
}

function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <p className="text-3xl font-semibold tracking-tight text-steel-600">404</p>
      <p className="mt-2 text-sm text-steel-400">This route does not exist.</p>
      <a href="/" className="mt-4 text-xs text-violet-300 hover:text-violet-200">
        Return to Overview
      </a>
    </div>
  )
}

export default function App() {
  const { user } = useAuth()
  return (
    <Routes>
      <Route
        path="/login"
        element={user ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<OverviewPage />} />
        <Route path="director" element={<DirectorPage />} />
        <Route path="strategy-director" element={<StrategyDirectorPage />} />
        <Route path="research" element={<ResearchCentrePage />} />
        <Route path="strategy" element={<StrategyStudioPage />} />
        <Route path="campaigns" element={<CampaignsPage />} />
        <Route path="campaigns/:id" element={<CampaignDetailPage />} />
        <Route path="calendar" element={<ContentCalendarPage />} />
        <Route path="content/:id" element={<ContentDetailPage />} />
        <Route path="creative" element={<CreativeStudioPage />} />
        <Route path="approvals" element={<ApprovalCentrePage />} />
        <Route path="social" element={<SocialAccountsPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="brand" element={<BrandKnowledgePage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

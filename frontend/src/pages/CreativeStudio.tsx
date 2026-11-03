import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { creativeStudioApi } from '../lib/api'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import { Banner, Mono, Skeleton, Tabs } from '../components/ui'
import { CreativeOverviewPanel } from './creative/CreativeOverviewPanel'
import { CreativeBriefEditor } from './creative/CreativeBriefEditor'
import { CreativeGeneration } from './creative/CreativeGeneration'
import { CreativeAssetLibrary } from './creative/CreativeAssetLibrary'

type TabId = 'overview' | 'briefs' | 'generation' | 'assets'

const TAB_IDS: TabId[] = ['overview', 'briefs', 'generation', 'assets']

/**
 * Creative Studio (Phase 3B).
 *
 * The shell owns the provider capability banner: every tab is told, in the UI
 * itself, that generation runs on the deterministic mock and that nothing here
 * publishes to a network.
 */
export function CreativeStudioPage() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab') as TabId | null
  const initial: TabId = requested && TAB_IDS.includes(requested) ? requested : 'overview'
  const [tab, setTab] = useState<TabId>(initial)

  useEffect(() => {
    const next = params.get('tab') as TabId | null
    if (next && TAB_IDS.includes(next) && next !== tab) setTab(next)
  }, [params, tab])

  function change(next: TabId) {
    setTab(next)
    const updated = new URLSearchParams(params)
    updated.set('tab', next)
    setParams(updated, { replace: true })
  }

  const capabilities = useApi(() => creativeStudioApi.capabilities())
  const caps = capabilities.data

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Creative"
        title="Creative Studio"
        description="Turn an approved content version into a structured brief, run a mock generation job, and store the result as a versioned asset. Approval covers the exact content version, brief version and asset version you choose — nothing here publishes."
        actions={
          <div className="flex items-center gap-2 text-[11px] text-steel-500">
            <span>provider</span>
            <Mono>{caps ? caps.provider : '…'}</Mono>
          </div>
        }
      />

      {capabilities.error ? (
        <ErrorState message={capabilities.error.message} onRetry={capabilities.reload} />
      ) : capabilities.loading ? (
        <Skeleton className="h-16 w-full" />
      ) : caps ? (
        <>
          {caps.is_mock ? (
            <Banner tone="warn" title="Mock generation is active">
              Jobs are executed by the deterministic <Mono>{caps.provider}</Mono> provider. Every asset
              it produces is a synthetic abstract frame — not model-generated artwork and not a
              screenshot of Crusor software. Video jobs produce a poster frame, not an encoded video.
            </Banner>
          ) : null}
          {caps.flow_connector.status === 'not_available' ? (
            <Banner tone="neutral" title="Google Flow is not available">
              The Flow Connector source is absent from this repository, so no adapter is active and
              no request is attempted. {caps.flow_connector.required_information.length} pieces of
              information are still required before it can be implemented. Paid generation is{' '}
              <strong>{caps.paid_generation_enabled ? 'authorised' : 'disabled'}</strong> and this
              build has no publishing endpoint.
            </Banner>
          ) : null}
        </>
      ) : null}

      <Tabs
        value={tab}
        onChange={change}
        tabs={[
          { id: 'overview', label: 'Overview' },
          { id: 'briefs', label: 'Brief editor' },
          { id: 'generation', label: 'Generation centre' },
          { id: 'assets', label: 'Asset library' },
        ]}
      />

      {tab === 'overview' ? <CreativeOverviewPanel /> : null}
      {tab === 'briefs' ? <CreativeBriefEditor /> : null}
      {tab === 'generation' ? <CreativeGeneration /> : null}
      {tab === 'assets' ? <CreativeAssetLibrary /> : null}
    </div>
  )
}

import { useRef, useState } from 'react'
import { useMutation, useApi } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, creativeApi } from '../lib/api'
import type { CreativeAsset } from '../lib/types'
import { formatBytes, formatDateTime } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  Skeleton,
} from '../components/ui'
import { IconStudio } from '../components/layout/icons'

export function CreativeStudioPage() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const assets = useApi(() => creativeApi.list())
  const fileRef = useRef<HTMLInputElement>(null)
  const replaceRef = useRef<HTMLInputElement>(null)
  const [replacing, setReplacing] = useState<CreativeAsset | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const upload = useMutation(async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) throw new Error('Choose a file first.')
    return creativeApi.upload({ file, kind: 'image' })
  })

  const replace = useMutation(async () => {
    const file = replaceRef.current?.files?.[0]
    if (!file || !replacing) throw new Error('Choose a replacement file.')
    return creativeApi.replace(replacing.id, file)
  })

  async function doUpload() {
    setUploadError(null)
    const result = await upload.run(undefined as never)
    if (result) {
      notify(`Uploaded ${result.filename}.`, 'ok')
      assets.reload()
      if (fileRef.current) fileRef.current.value = ''
    } else {
      setUploadError(upload.error?.message ?? 'Upload failed.')
    }
  }

  async function doReplace() {
    const result = await replace.run(undefined as never)
    if (result) {
      notify(result.message, result.invalidated_approvals > 0 ? 'neutral' : 'ok')
      assets.reload()
      setReplacing(null)
      if (replaceRef.current) replaceRef.current.value = ''
    }
  }

  const visual = (brand.data?.visual_direction ?? {}) as Record<string, string>

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Assets"
        title="Creative Studio"
        description="Register creative assets, upload bytes, and swap them safely. Replacing an asset that carries a live approval revokes that approval immediately."
      />

      <Banner tone="neutral" title="Image generation is not configured">
        No image model is wired in Phase 1. Rather than fabricate assets, the Creative Agent produces
        an executable art-direction brief; assets are uploaded here by a human. Provider status:{' '}
        <Mono>not_configured</Mono>
      </Banner>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader
            title="Asset library"
            subtitle={assets.data ? `${assets.data.length} asset(s)` : undefined}
            action={
              <div className="flex items-center gap-2">
                <input
                  ref={fileRef}
                  type="file"
                  className="hq-input max-w-[190px] py-1 text-[11px]"
                  disabled={!canEdit}
                />
                <Button size="sm" variant="primary" loading={upload.pending} onClick={doUpload}>
                  Upload
                </Button>
              </div>
            }
          />
          {uploadError ? (
            <div className="px-5 pt-4">
              <Banner tone="danger">{uploadError}</Banner>
            </div>
          ) : null}
          {assets.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : assets.error ? (
            <div className="p-4">
              <ErrorState message={assets.error.message} onRetry={assets.reload} />
            </div>
          ) : (assets.data ?? []).length === 0 ? (
            <EmptyState
              icon={<IconStudio />}
              title="No creative assets"
              description="Upload a file to register its checksum and storage key. Assets attached to content become part of the approval surface."
            />
          ) : (
            <table className="hq-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Kind</th>
                  <th>Size</th>
                  <th>Checksum</th>
                  <th>Created</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(assets.data ?? []).map((asset) => (
                  <tr key={asset.id}>
                    <td className="max-w-[220px] truncate">{asset.filename}</td>
                    <td>
                      <Badge tone="muted">{asset.kind}</Badge>
                    </td>
                    <td className="hq-mono">{formatBytes(asset.size_bytes)}</td>
                    <td>
                      <Mono>{asset.checksum ? `${asset.checksum.slice(0, 10)}…` : '—'}</Mono>
                    </td>
                    <td className="text-steel-500">{formatDateTime(asset.created_at)}</td>
                    <td className="text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!canEdit}
                        onClick={() => setReplacing(asset)}
                      >
                        Replace
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card className="self-start">
          <CardHeader
            title="Visual direction"
            subtitle="From the brand knowledge base — the agent briefs against this."
          />
          <div className="px-5 py-4">
            {brand.loading ? (
              <Skeleton className="h-32 w-full" />
            ) : brand.error ? (
              <ErrorState message={brand.error.message} onRetry={brand.reload} />
            ) : Object.keys(visual).length === 0 ? (
              <EmptyState
                compact
                title="No visual direction"
                description="Set backgrounds, accent and metallic colours in Brand Knowledge."
              />
            ) : (
              <div className="space-y-2.5">
                {Object.entries(visual).map(([key, value]) => (
                  <div key={key} className="flex items-start justify-between gap-3">
                    <span className="text-[11px] uppercase tracking-brand text-steel-600">
                      {key.replace(/_/g, ' ')}
                    </span>
                    <span className="max-w-[60%] text-right text-[11px] text-steel-300">
                      {String(value)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      </div>

      {replacing ? (
        <Card>
          <CardHeader
            title={`Replace “${replacing.filename}”`}
            subtitle="A new checksum invalidates any approval bound to this asset."
            action={
              <Button size="sm" variant="ghost" onClick={() => setReplacing(null)}>
                Cancel
              </Button>
            }
          />
          <div className="flex flex-wrap items-center gap-3 px-5 py-4">
            <input ref={replaceRef} type="file" className="hq-input max-w-xs py-1 text-[11px]" />
            <Button size="sm" variant="danger" loading={replace.pending} onClick={doReplace}>
              Replace asset
            </Button>
            {replace.error ? (
              <span className="text-[11px] text-signal-danger">{replace.error.message}</span>
            ) : null}
          </div>
        </Card>
      ) : null}
    </div>
  )
}

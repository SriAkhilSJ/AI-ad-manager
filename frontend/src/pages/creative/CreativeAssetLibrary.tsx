import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../../hooks/useApi'
import { useAuth } from '../../hooks/useAuth'
import { useToast } from '../../hooks/useToast'
import { creativeStudioApi } from '../../lib/api'
import type { CreativeAssetStudio } from '../../lib/types'
import { formatBytes, formatDateTime } from '../../lib/format'
import { ErrorState } from '../../components/layout/PageHeader'
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
} from '../../components/ui'
import { useAssetPreview } from './useAssetPreview'

export function validationTone(status: string): 'ok' | 'warn' | 'danger' | 'muted' {
  if (status === 'pass') return 'ok'
  if (status === 'pass_with_warnings') return 'warn'
  if (status === 'needs_revision') return 'warn'
  if (status === 'blocked') return 'danger'
  return 'muted'
}

function AssetThumb({ asset }: { asset: CreativeAssetStudio }) {
  const { url, error } = useAssetPreview(asset.id)
  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-[10px] text-steel-600">
        preview unavailable
      </div>
    )
  }
  if (!url) return <Skeleton className="h-full w-full" />
  return (
    <img
      src={url}
      alt={asset.filename}
      className="h-full w-full object-cover"
      loading="lazy"
      draggable={false}
    />
  )
}

/**
 * Asset library: grid or list, searchable, filterable, with honest validation
 * badges and a mock label on every synthetic asset.
 */
export function CreativeAssetLibrary() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const [view, setView] = useState<'grid' | 'list'>('grid')
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')
  const [validation, setValidation] = useState('')
  const [platform, setPlatform] = useState('')
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const assets = useApi(
    () =>
      creativeStudioApi.assets({
        q: query || undefined,
        kind: kind || undefined,
        validation_status: validation || undefined,
        platform: platform || undefined,
        limit: 100,
      }),
    [query, kind, validation, platform],
  )

  const upload = useMutation(async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) throw new Error('Choose a file first.')
    return creativeStudioApi.uploadAsset({ file, kind: 'image' })
  })

  async function doUpload() {
    setUploadError(null)
    const result = await upload.run(undefined as never)
    if (result) {
      notify(`Uploaded ${result.filename} (${formatBytes(result.size_bytes)}).`, 'ok')
      assets.reload()
      if (fileRef.current) fileRef.current.value = ''
    } else {
      setUploadError(upload.error?.message ?? 'Upload failed.')
    }
  }

  const rows = assets.data ?? []

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Filters"
          subtitle="Search runs server-side over filenames and prompts."
          action={
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant={view === 'grid' ? 'secondary' : 'ghost'}
                onClick={() => setView('grid')}
              >
                Grid
              </Button>
              <Button
                size="sm"
                variant={view === 'list' ? 'secondary' : 'ghost'}
                onClick={() => setView('list')}
              >
                List
              </Button>
            </div>
          }
        />
        <div className="flex flex-wrap items-end gap-3 px-5 py-4">
          <label className="block min-w-[220px] flex-1">
            <span className="hq-label">Search</span>
            <Input
              className="mt-1 w-full"
              value={query}
              placeholder="filename or prompt"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label className="block">
            <span className="hq-label">Kind</span>
            <Select
              className="mt-1"
              value={kind}
              onChange={(event) => setKind(event.target.value)}
            >
              <option value="">any</option>
              <option value="image">image</option>
              <option value="video">video</option>
            </Select>
          </label>
          <label className="block">
            <span className="hq-label">Validation</span>
            <Select
              className="mt-1"
              value={validation}
              onChange={(event) => setValidation(event.target.value)}
            >
              <option value="">any</option>
              <option value="pass">pass</option>
              <option value="pass_with_warnings">pass with warnings</option>
              <option value="needs_revision">needs revision</option>
              <option value="blocked">blocked</option>
              <option value="unverified">unverified</option>
            </Select>
          </label>
          <label className="block">
            <span className="hq-label">Platform</span>
            <Select
              className="mt-1"
              value={platform}
              onChange={(event) => setPlatform(event.target.value)}
            >
              <option value="">any</option>
              <option value="instagram">instagram</option>
              <option value="linkedin">linkedin</option>
              <option value="youtube">youtube</option>
              <option value="x">x</option>
              <option value="facebook">facebook</option>
              <option value="reddit">reddit</option>
            </Select>
          </label>
          <div className="ml-auto flex items-end gap-2">
            <input ref={fileRef} type="file" className="hq-input max-w-[190px] py-1 text-[11px]" />
            <Button
              size="sm"
              variant="primary"
              loading={upload.pending}
              disabled={!canEdit}
              onClick={doUpload}
            >
              Upload file
            </Button>
          </div>
        </div>
        {uploadError ? (
          <div className="px-5 pb-4">
            <Banner tone="danger">{uploadError}</Banner>
          </div>
        ) : null}
        <div className="border-t border-white/[0.06] px-5 py-3 text-[11px] text-steel-600">
          Uploads are validated against the file's real signature, not the browser's claimed type.
          The original bytes are never overwritten — a replacement becomes a new asset version.
        </div>
      </Card>

      {assets.error ? (
        <ErrorState message={assets.error.message} onRetry={assets.reload} />
      ) : assets.loading ? (
        <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-4">
          <Skeleton className="h-52 w-full" />
          <Skeleton className="h-52 w-full" />
          <Skeleton className="h-52 w-full" />
          <Skeleton className="h-52 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <Card>
          <EmptyState
            title="No assets match"
            description="Generate one from the Generation centre, or upload an existing file. Every asset records a checksum, dimensions and validation status."
          />
        </Card>
      ) : view === 'grid' ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((asset) => (
            <Card key={asset.id} className="overflow-hidden">
              <div className="relative aspect-[4/3] w-full bg-black/40">
                <AssetThumb asset={asset} />
                <div className="absolute left-2 top-2 flex flex-wrap gap-1">
                  {asset.is_mock ? <Badge tone="warn">MOCK</Badge> : null}
                  {asset.selected ? <Badge tone="violet">selected</Badge> : null}
                </div>
                <div className="absolute right-2 top-2">
                  <Badge tone={validationTone(asset.validation_status)}>
                    {asset.validation_status.replace(/_/g, ' ')}
                  </Badge>
                </div>
              </div>
              <div className="space-y-2 px-4 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-xs text-steel-300">{asset.filename}</p>
                    <p className="mt-0.5 text-[11px] text-steel-600">
                      v{asset.asset_version} · {asset.width ?? '?'}×{asset.height ?? '?'} ·{' '}
                      {formatBytes(asset.size_bytes)}
                    </p>
                  </div>
                  <Link
                    to={`/creative/assets/${asset.id}`}
                    className="shrink-0 text-[11px] text-violet-300 hover:text-violet-200"
                  >
                    open
                  </Link>
                </div>
                {asset.is_mock && asset.mock_label ? (
                  <p className="line-clamp-2 text-[10px] leading-relaxed text-steel-600">
                    {asset.mock_label}
                  </p>
                ) : null}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="hq-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Kind</th>
                  <th>Version</th>
                  <th>Size</th>
                  <th>Validation</th>
                  <th>Checksum</th>
                  <th>Created</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((asset) => (
                  <tr key={asset.id}>
                    <td className="max-w-[220px]">
                      <div className="truncate text-xs text-steel-300">{asset.filename}</div>
                      {asset.is_mock ? (
                        <span className="text-[10px] text-steel-600">mock asset</span>
                      ) : null}
                    </td>
                    <td>
                      <Badge tone="muted">{asset.kind}</Badge>
                    </td>
                    <td className="hq-mono text-steel-500">v{asset.asset_version}</td>
                    <td className="hq-mono text-steel-500">{formatBytes(asset.size_bytes)}</td>
                    <td>
                      <Badge tone={validationTone(asset.validation_status)}>
                        {asset.validation_status.replace(/_/g, ' ')}
                      </Badge>
                    </td>
                    <td>
                      <Mono>{asset.checksum.slice(0, 10)}…</Mono>
                    </td>
                    <td className="text-steel-500">{formatDateTime(asset.created_at)}</td>
                    <td className="text-right">
                      <Link
                        to={`/creative/assets/${asset.id}`}
                        className="text-[11px] text-violet-300 hover:text-violet-200"
                      >
                        detail
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}

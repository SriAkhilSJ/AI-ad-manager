import { useState } from 'react'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, socialApi } from '../lib/api'
import { PLATFORMS, type Platform } from '../lib/types'
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
  Modal,
  Mono,
  Select,
  Skeleton,
} from '../components/ui'
import { PlatformChip, PlatformMark } from '../components/shared'
import { IconPlug, IconSocial } from '../components/layout/icons'

export function SocialAccountsPage() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const platforms = useApi(() => socialApi.platforms())
  const accounts = useApi(() => socialApi.accounts())
  const published = useApi(() => socialApi.published())
  const [addOpen, setAddOpen] = useState(false)

  const revoke = useMutation((accountId: string) => socialApi.revoke(accountId))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Distribution"
        title="Social Accounts"
        description="Account metadata only. OAuth tokens are never stored in application tables and never returned by the API — connectors keep them in an external vault and persist an opaque reference."
        actions={
          <Button
            variant="primary"
            disabled={!canEdit || !brand.data}
            onClick={() => setAddOpen(true)}
          >
            <IconPlug size={14} /> Link account
          </Button>
        }
      />

      <Banner tone="neutral" title="Phase 1: no network publishing">
        All six platforms are modelled, and every one reports{' '}
        <Mono>publish_enabled: false</Mono>. Approval and scheduling are fully implemented; the
        transport that would send a post is a Phase 2 integration.
      </Banner>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Platform readiness"
            subtitle="Driven by environment configuration, reported honestly."
          />
          {platforms.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : platforms.error ? (
            <div className="p-4">
              <ErrorState message={platforms.error.message} onRetry={platforms.reload} />
            </div>
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {(platforms.data ?? []).map((row) => (
                <li key={row.platform} className="px-5 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <PlatformMark platform={row.platform} />
                        <span className="text-xs font-medium text-steel-300">
                          {titleCase(row.platform)}
                        </span>
                      </div>
                      <p className="mt-1 text-[11px] leading-relaxed text-steel-600">{row.reason}</p>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1">
                      <Badge tone={row.publish_enabled ? 'ok' : 'muted'}>
                        {row.publish_enabled ? 'publish enabled' : 'gated'}
                      </Badge>
                      <Mono>
                        {row.credentials_configured ? 'creds set' : 'no creds'}
                      </Mono>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader
            title="Accounts"
            subtitle={accounts.data ? `${accounts.data.length} record(s)` : undefined}
            action={
              <Button size="sm" variant="ghost" onClick={accounts.reload}>
                Refresh
              </Button>
            }
          />
          {accounts.loading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : accounts.error ? (
            <div className="p-4">
              <ErrorState message={accounts.error.message} onRetry={accounts.reload} />
            </div>
          ) : (accounts.data ?? []).length === 0 ? (
            <EmptyState
              icon={<IconSocial />}
              title="No accounts linked"
              description="Register an account handle to associate future publications. Nothing is sent anywhere in Phase 1."
              action={
                canEdit && brand.data ? (
                  <Button size="sm" onClick={() => setAddOpen(true)}>
                    Link account
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <ul className="divide-y divide-white/[0.04]">
              {(accounts.data ?? []).map((account) => (
                <li key={account.id} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <PlatformChip platform={account.platform} />
                      <span className="truncate text-xs text-steel-300">
                        {account.handle || '—'}
                      </span>
                    </div>
                    <p className="mt-1 hq-mono">
                      {account.status} · linked {formatDateTime(account.created_at)}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge tone={account.status === 'connected' ? 'ok' : 'muted'} dot>
                      {account.status}
                    </Badge>
                    {canEdit ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={account.status === 'revoked'}
                        onClick={async () => {
                          const result = await revoke.run(account.id)
                          if (result) {
                            notify('Account link revoked.', 'neutral')
                            accounts.reload()
                          }
                        }}
                      >
                        Revoke
                      </Button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Publication records"
          subtitle="Every row names the approval that authorised it."
        />
        {published.loading ? (
          <div className="p-4">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : (published.data ?? []).length === 0 ? (
          <EmptyState
            compact
            title="Nothing published"
            description="No publication has been recorded. With no platform transport configured, none can be."
          />
        ) : (
          <table className="hq-table">
            <thead>
              <tr>
                <th>Platform</th>
                <th>External ID</th>
                <th>Approval</th>
                <th>Published</th>
              </tr>
            </thead>
            <tbody>
              {(published.data ?? []).map((post) => (
                <tr key={post.id}>
                  <td>
                    <PlatformChip platform={post.platform} />
                  </td>
                  <td>
                    <Mono>{post.external_post_id}</Mono>
                  </td>
                  <td>
                    <Mono>{post.approval_record_id.slice(0, 8)}…</Mono>
                  </td>
                  <td className="text-steel-500">{formatDateTime(post.published_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <LinkAccountModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        brandId={brand.data?.id}
        onCreated={() => {
          accounts.reload()
          notify('Account registered. Configure OAuth in Phase 2 to enable publishing.', 'ok')
        }}
      />
    </div>
  )
}

function LinkAccountModal({
  open,
  onClose,
  brandId,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  brandId?: string
  onCreated: () => void
}) {
  const [platform, setPlatform] = useState<Platform>('linkedin')
  const [handle, setHandle] = useState('')
  const create = useMutation(() =>
    socialApi.createAccount({
      brand_profile_id: brandId as string,
      platform,
      handle,
    }),
  )

  async function submit() {
    if (!brandId || !handle.trim()) return
    const result = await create.run(undefined as never)
    if (result) {
      setHandle('')
      onCreated()
      onClose()
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Link a social account"
      description="Records non-secret metadata only. This endpoint cannot accept or store tokens."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.pending} onClick={submit}>
            Register
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Select
          label="Platform"
          value={platform}
          onChange={(event) => setPlatform(event.target.value as Platform)}
        >
          {PLATFORMS.map((value) => (
            <option key={value} value={value}>
              {titleCase(value)}
            </option>
          ))}
        </Select>
        <Input
          label="Handle"
          value={handle}
          onChange={(event) => setHandle(event.target.value)}
          placeholder="crusor-ai"
        />
        {create.error ? <Banner tone="danger">{create.error.message}</Banner> : null}
      </div>
    </Modal>
  )
}

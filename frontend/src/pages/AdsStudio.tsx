import { useMemo, useState } from 'react'
import { adsApi, brandApi } from '../lib/api'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import type { AdCampaign, AdCampaignDetail } from '../lib/types'
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
} from '../components/ui'

/**
 * Paid advertising — governance view.
 *
 * The screen is deliberately about *approval*, not about launching. It shows the
 * proposed campaign, the exact hash a founder is being asked to authorise, the
 * authorisation ledger and every attempted mutation including refusals. A
 * transport that is not implemented says so instead of offering a button that
 * would pretend to work.
 */
export function AdsStudioPage() {
  const { user, canApprove } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const transports = useApi(() => adsApi.transports())
  const campaigns = useApi(() =>
    brand.data ? adsApi.campaigns(brand.data.id) : Promise.resolve([] as AdCampaign[]),
  )
  const [selected, setSelected] = useState<string | null>(null)
  const detail = useApi(() =>
    selected ? adsApi.detail(selected) : Promise.resolve(null as AdCampaignDetail | null),
  )

  const [platform, setPlatform] = useState('meta_ads')
  const [name, setName] = useState('')
  const [daily, setDaily] = useState('2000')
  const [total, setTotal] = useState('50000')
  const propose = useMutation(() =>
    adsApi.propose({
      brand_profile_id: brand.data?.id as string,
      platform,
      name: name || 'Untitled proposal',
      objective: 'traffic',
      plan: { source: 'operator' },
      targeting: {},
      daily_budget_cents: Number(daily || 0),
      total_budget_cents: Number(total || 0),
    }),
  )

  const [spend, setSpend] = useState('')
  const authorize = useMutation(() =>
    adsApi.authorize(selected as string, {
      max_spend_cents: Number(spend || 0),
      allowed_operations: ['create', 'launch', 'pause', 'resume'],
      ttl_hours: 24,
    }),
  )
  const execute = useMutation((authorizationId: string) =>
    adsApi.execute(selected as string, { kind: 'create', authorization_id: authorizationId }),
  )

  const transportRows = transports.data ?? []
  const anyTransport = useMemo(() => transportRows.some((row) => row.available), [transportRows])

  async function submitProposal() {
    const created = await propose.run(undefined as never)
    if (created) {
      notify('Proposal drafted. Nothing external exists until a founder authorises it.', 'ok')
      campaigns.reload()
      setSelected(created.id)
    }
  }

  async function submitAuthorization(campaign: AdCampaign) {
    if (!selected) return
    const auth = await authorize.run(undefined as never)
    if (auth) {
      notify('Authorisation recorded for this exact proposal hash.', 'ok')
      detail.reload()
      campaigns.reload()
      if (campaign) setSpend('')
    }
  }

  async function attempt(authorizationId: string) {
    const op = await execute.run(authorizationId)
    if (op) {
      if (op.outcome === 'refused') notify(`Refused: ${op.detail}`, 'danger')
      else if (op.outcome === 'succeeded') notify('The platform confirmed the change.', 'ok')
      else notify(`Outcome: ${op.outcome} — ${op.detail}`, 'neutral')
      detail.reload()
      campaigns.reload()
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Paid media"
        title="Advertising"
        description="Agents may draft a paid campaign; only a founder may authorise spend, and only for the exact proposal hash shown here. Every attempted external change — including a refusal — is written to an append-only ledger."
      />

      {!anyTransport ? (
        <Banner tone="neutral" title="No ads transport is implemented in this build">
          The proposal → authorisation → ledger chain is real and enforced. The platform
          transports are not, so a launch is refused and recorded rather than attempted. Nothing
          here can spend money.
        </Banner>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[1fr_1.4fr]">
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Transports"
              subtitle="Reported honestly: implemented is not the same as available."
            />
            {transports.loading ? (
              <div className="p-4">
                <Skeleton className="h-16 w-full" />
              </div>
            ) : (
              <ul className="divide-y divide-white/[0.04]">
                {transportRows.map((row) => (
                  <li key={row.platform} className="px-5 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-xs font-medium text-steel-300">
                          {titleCase(row.platform.replace('_', ' '))}
                        </div>
                        <p className="mt-1 text-[11px] leading-relaxed text-steel-600">
                          {row.reason}
                        </p>
                      </div>
                      <Badge tone={row.available ? 'ok' : 'muted'}>
                        {row.available ? 'available' : 'unavailable'}
                      </Badge>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card>
            <CardHeader title="Draft a proposal" subtitle="Cannot spend. Creates a local draft only." />
            <div className="space-y-3 p-5">
              <Select label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
                <option value="meta_ads">Meta Ads</option>
                <option value="google_ads">Google Ads</option>
                <option value="linkedin_ads">LinkedIn Ads</option>
              </Select>
              <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Q4 launch push" />
              <div className="grid grid-cols-2 gap-3">
                <Input
                  label="Daily budget (cents)"
                  value={daily}
                  onChange={(e) => setDaily(e.target.value)}
                />
                <Input
                  label="Total budget (cents)"
                  value={total}
                  onChange={(e) => setTotal(e.target.value)}
                />
              </div>
              {propose.error ? <Banner tone="danger">{propose.error.message}</Banner> : null}
              <Button
                variant="primary"
                loading={propose.pending}
                disabled={!brand.data || !name.trim()}
                onClick={submitProposal}
              >
                Draft proposal
              </Button>
            </div>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Proposals"
              subtitle={campaigns.data ? `${campaigns.data.length} record(s)` : undefined}
              action={
                <Button size="sm" variant="ghost" onClick={campaigns.reload}>
                  Refresh
                </Button>
              }
            />
            {campaigns.loading ? (
              <div className="p-4">
                <Skeleton className="h-20 w-full" />
              </div>
            ) : campaigns.error ? (
              <div className="p-4">
                <ErrorState message={campaigns.error.message} onRetry={campaigns.reload} />
              </div>
            ) : (campaigns.data ?? []).length === 0 ? (
              <EmptyState
                compact
                title="No proposals"
                description="Draft one on the left. Until a founder authorises it, the system has no permission to touch an ad account."
              />
            ) : (
              <ul className="divide-y divide-white/[0.04]">
                {(campaigns.data ?? []).map((campaign) => (
                  <li
                    key={campaign.id}
                    className={`cursor-pointer px-5 py-3 hover:bg-white/[0.02] ${
                      selected === campaign.id ? 'bg-white/[0.03]' : ''
                    }`}
                    onClick={() => setSelected(campaign.id)}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-medium text-steel-300">
                          {campaign.name}
                        </div>
                        <p className="mt-1 hq-mono truncate">
                          {titleCase(campaign.platform.replace('_', ' '))} ·{' '}
                          {campaign.currency} {(campaign.total_budget_cents / 100).toFixed(2)} · hash{' '}
                          {campaign.proposal_hash.slice(0, 10)}…
                        </p>
                      </div>
                      <Badge
                        tone={
                          campaign.status === 'authorized' || campaign.status === 'active'
                            ? 'ok'
                            : campaign.status === 'rejected' || campaign.status === 'failed'
                              ? 'danger'
                              : 'muted'
                        }
                        dot
                      >
                        {campaign.status}
                      </Badge>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {selected ? (
            <Card>
              <CardHeader
                title="Authorisation & ledger"
                subtitle="Bound to the exact proposal hash; single use; expiring."
              />
              {detail.loading ? (
                <div className="p-4">
                  <Skeleton className="h-24 w-full" />
                </div>
              ) : detail.data ? (
                <div className="space-y-4 p-5">
                  <div className="rounded-lg border border-white/[0.06] p-3">
                    <div className="flex items-center justify-between">
                      <span className="hq-label">Proposal hash</span>
                      <Mono>{detail.data.campaign.proposal_hash.slice(0, 24)}…</Mono>
                    </div>
                    <div className="mt-2 flex items-center justify-between">
                      <span className="hq-label">Transport</span>
                      <Badge tone={detail.data.transport.available ? 'ok' : 'muted'}>
                        {detail.data.transport.available ? 'available' : 'unavailable'}
                      </Badge>
                    </div>
                    <p className="mt-2 text-[11px] leading-relaxed text-steel-600">
                      {detail.data.transport.reason}
                    </p>
                  </div>

                  {canApprove ? (
                    <div className="space-y-2">
                      <Input
                        label="Authorise maximum spend (cents)"
                        value={spend}
                        onChange={(e) => setSpend(e.target.value)}
                        placeholder={String(detail.data.campaign.total_budget_cents)}
                      />
                      {authorize.error ? <Banner tone="danger">{authorize.error.message}</Banner> : null}
                      <Button
                        variant="primary"
                        loading={authorize.pending}
                        disabled={Number(spend || 0) <= 0}
                        onClick={() => submitAuthorization(detail.data!.campaign)}
                      >
                        Authorise this proposal
                      </Button>
                    </div>
                  ) : (
                    <Banner tone="neutral">
                      Only a founder may authorise advertising spend. You are signed in as{' '}
                      <Mono>{user?.role ?? 'unknown'}</Mono>.
                    </Banner>
                  )}

                  <div>
                    <p className="hq-label mb-2">Authorisations</p>
                    {detail.data.authorizations.length === 0 ? (
                      <p className="text-[11px] text-steel-600">None. Nothing can be launched.</p>
                    ) : (
                      <ul className="space-y-2">
                        {detail.data.authorizations.map((auth) => (
                          <li
                            key={auth.id}
                            className="rounded-lg border border-white/[0.06] px-3 py-2"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <Badge tone={auth.status === 'active' ? 'ok' : 'muted'} dot>
                                {auth.status}
                              </Badge>
                              <Mono>≤ {auth.max_spend_cents} {auth.currency}</Mono>
                            </div>
                            <p className="mt-1 hq-mono">
                              expires {formatDateTime(auth.expires_at)} · ops{' '}
                              {auth.allowed_operations.join(', ')}
                            </p>
                            {canApprove && auth.status === 'active' ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                className="mt-2"
                                loading={execute.pending}
                                onClick={() => attempt(auth.id)}
                              >
                                Attempt create
                              </Button>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  <div>
                    <p className="hq-label mb-2">Operation ledger (append-only)</p>
                    {detail.data.operations.length === 0 ? (
                      <p className="text-[11px] text-steel-600">
                        No external mutation has been attempted.
                      </p>
                    ) : (
                      <table className="hq-table">
                        <thead>
                          <tr>
                            <th>Kind</th>
                            <th>Outcome</th>
                            <th>Detail</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.data.operations.map((op) => (
                            <tr key={op.id}>
                              <td>
                                <Mono>{op.kind}</Mono>
                              </td>
                              <td>
                                <Badge
                                  tone={
                                    op.outcome === 'succeeded'
                                      ? 'ok'
                                      : op.outcome === 'refused'
                                        ? 'warn'
                                        : 'danger'
                                  }
                                >
                                  {op.outcome}
                                </Badge>
                              </td>
                              <td className="text-[11px] text-steel-500">{op.detail}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </div>
              ) : null}
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  )
}

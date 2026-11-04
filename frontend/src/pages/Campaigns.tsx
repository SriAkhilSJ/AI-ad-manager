import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { brandApi, campaignApi } from '../lib/api'
import type { Campaign } from '../lib/types'
import { formatDate, titleCase } from '../lib/format'
import { ErrorState, PageHeader } from '../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  EmptyState,
  Input,
  Modal,
  Mono,
  Select,
  Skeleton,
  Tabs,
  Textarea,
} from '../components/ui'
import { PlatformChip } from '../components/shared'
import { IconCampaign, IconPlus } from '../components/layout/icons'

const STATUS_TONE: Record<string, 'ok' | 'info' | 'warn' | 'muted'> = {
  draft: 'muted',
  planned: 'info',
  active: 'ok',
  paused: 'warn',
  completed: 'info',
  archived: 'muted',
}

type Filter = 'all' | 'draft' | 'planned' | 'active' | 'completed'

export function CampaignsPage() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const brand = useApi(() => brandApi.active())
  const [filter, setFilter] = useState<Filter>('all')
  const campaigns = useApi(
    () => campaignApi.list(filter === 'all' ? undefined : { status_filter: filter }),
    [filter],
  )
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Planning"
        title="Campaigns"
        description="Campaigns hold the objective, channel mix and the structured strategy plan produced by the Strategy Agent."
        actions={
          <Button
            variant="primary"
            disabled={!canEdit || !brand.data}
            onClick={() => setCreateOpen(true)}
          >
            <IconPlus size={14} /> New campaign
          </Button>
        }
      >
        <Tabs<Filter>
          value={filter}
          onChange={setFilter}
          tabs={[
            { id: 'all', label: 'All' },
            { id: 'draft', label: 'Draft' },
            { id: 'planned', label: 'Planned' },
            { id: 'active', label: 'Active' },
            { id: 'completed', label: 'Completed' },
          ]}
        />
      </PageHeader>

      {brand.error ? (
        <Banner tone="warn" title="Brand profile required">
          Create or seed a brand profile before planning campaigns.
        </Banner>
      ) : null}

      {campaigns.loading ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-40 w-full" />
          ))}
        </div>
      ) : campaigns.error ? (
        <ErrorState message={campaigns.error.message} onRetry={campaigns.reload} />
      ) : (campaigns.data ?? []).length === 0 ? (
        <Card>
          <EmptyState
            icon={<IconCampaign />}
            title="No campaigns yet"
            description="A campaign is created automatically when you run a Director workflow, or you can create one manually and attach content to it."
            action={
              canEdit && brand.data ? (
                <Button variant="primary" size="sm" onClick={() => setCreateOpen(true)}>
                  Create campaign
                </Button>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {(campaigns.data ?? []).map((campaign) => (
            <CampaignCard key={campaign.id} campaign={campaign} />
          ))}
        </div>
      )}

      <CreateCampaignModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        brandId={brand.data?.id}
        onCreated={() => {
          campaigns.reload()
          notify('Campaign created.', 'ok')
        }}
      />
    </div>
  )
}

function CampaignCard({ campaign }: { campaign: Campaign }) {
  const pillars = (campaign.plan?.messaging_pillars as string[] | undefined) ?? []
  const plan = (campaign.plan?.content_plan as unknown[] | undefined) ?? []
  return (
    <Link to={`/campaigns/${campaign.id}`} className="block">
      <Card interactive className="h-full">
        <div className="flex h-full flex-col p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-steel-300">{campaign.name}</p>
              <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-steel-500">
                {campaign.objective || 'No objective set'}
              </p>
            </div>
            <Badge tone={STATUS_TONE[campaign.status] ?? 'muted'} dot>
              {titleCase(campaign.status)}
            </Badge>
          </div>

          <div className="mt-3 flex flex-wrap gap-1.5">
            {campaign.platforms.slice(0, 4).map((platform) => (
              <PlatformChip key={platform} platform={platform} />
            ))}
            {campaign.platforms.length === 0 ? (
              <span className="text-[11px] text-steel-600">No channels selected</span>
            ) : null}
          </div>

          <div className="mt-auto flex items-center justify-between gap-2 pt-4">
            <div className="flex flex-wrap gap-1">
              {pillars.slice(0, 2).map((pillar) => (
                <Badge key={pillar} tone="muted">
                  {pillar}
                </Badge>
              ))}
            </div>
            <Mono>
              {plan.length} slots · {formatDate(campaign.created_at)}
            </Mono>
          </div>
        </div>
      </Card>
    </Link>
  )
}

function CreateCampaignModal({
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
  const [name, setName] = useState('')
  const [objective, setObjective] = useState('')
  const [platform, setPlatform] = useState('linkedin')
  const create = useMutation(() =>
    campaignApi.create({
      brand_profile_id: brandId as string,
      name,
      objective,
      platforms: [platform],
    }),
  )

  async function submit() {
    if (!brandId || name.trim().length === 0) return
    const result = await create.run(undefined as never)
    if (result) {
      setName('')
      setObjective('')
      onCreated()
      onClose()
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New campaign"
      description="Campaigns created by the director include a full strategy plan. Manual campaigns start empty."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.pending} onClick={submit}>
            Create
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Input
          label="Name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Developer launch — Q4"
        />
        <Textarea
          label="Objective"
          value={objective}
          onChange={(event) => setObjective(event.target.value)}
          placeholder="What should this campaign achieve?"
        />
        <Select label="Primary channel" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          {['linkedin', 'x', 'instagram', 'youtube', 'facebook', 'reddit'].map((value) => (
            <option key={value} value={value}>
              {titleCase(value)}
            </option>
          ))}
        </Select>
        {create.error ? <Banner tone="danger">{create.error.message}</Banner> : null}
      </div>
    </Modal>
  )
}

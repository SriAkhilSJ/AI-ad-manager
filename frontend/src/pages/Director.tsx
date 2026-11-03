import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../hooks/useApi'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'
import { ApiError, directorApi, newIdempotencyKey } from '../lib/api'
import { PLATFORMS, type Platform, type WorkflowStatus } from '../lib/types'
import { relativeTime } from '../lib/format'
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
  SectionTitle,
  Skeleton,
  Textarea,
  VerdictBadge,
} from '../components/ui'
import { PlanView, QAFindings } from '../components/shared'
import { IconDirector, IconLock, IconRefresh } from '../components/layout/icons'

const WORKFLOW_STATUS_TONE: Record<
  string,
  'ok' | 'warn' | 'info' | 'danger' | 'muted'
> = {
  awaiting_approval: 'warn',
  completed: 'ok',
  executing: 'info',
  planning: 'info',
  initialized: 'muted',
  failed: 'danger',
  cancelled: 'muted',
}

export function DirectorPage() {
  const { canEdit, canApprove } = useAuth()
  const { notify } = useToast()
  const workflows = useApi(() => directorApi.workflows())
  const topology = useApi(() => directorApi.topology())
  const [selected, setSelected] = useState<string | null>(null)
  const [startOpen, setStartOpen] = useState(false)

  useEffect(() => {
    if (!selected && workflows.data && workflows.data.length > 0) {
      setSelected(workflows.data[0].workflow_id)
    }
  }, [workflows.data, selected])

  const detail = useApi(
    () => (selected ? directorApi.status(selected) : Promise.resolve(null as unknown as WorkflowStatus)),
    [selected],
    { enabled: Boolean(selected) },
  )

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="LangGraph supervisor"
        title="AI Marketing Director"
        description="The director grounds itself in the brand profile, plans a dependency-ordered task graph, delegates to specialised agents, runs the Brand Guardian QA gate, and then halts for founder approval."
        actions={
          <>
            <Button variant="secondary" onClick={() => { workflows.reload(); detail.reload() }}>
              <IconRefresh size={13} /> Refresh
            </Button>
            <Button variant="primary" disabled={!canEdit} onClick={() => setStartOpen(true)}>
              <IconDirector size={14} /> New workflow
            </Button>
          </>
        }
      />

      <Banner tone="info" title="Human in the loop">
        Workflows suspend at a real LangGraph <Mono>interrupt</Mono>. Resuming requires a founder
        decision; approving inside the director approves the exact persisted content versions. It
        never publishes.
      </Banner>

      <div className="grid gap-4 xl:grid-cols-[320px_1fr]">
        {/* workflow list */}
        <Card className="self-start">
          <CardHeader
            title="Workflows"
            subtitle={workflows.data ? `${workflows.data.length} thread(s)` : undefined}
          />
          {workflows.loading ? (
            <div className="space-y-2 p-3">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : workflows.error ? (
            <div className="p-3">
              <ErrorState message={workflows.error.message} onRetry={workflows.reload} />
            </div>
          ) : (workflows.data ?? []).length === 0 ? (
            <EmptyState
              compact
              title="No workflows"
              description="Start one to give the director an objective."
            />
          ) : (
            <ul className="max-h-[520px] divide-y divide-white/[0.04] overflow-y-auto">
              {(workflows.data ?? []).map((workflow) => (
                <li key={workflow.workflow_id}>
                  <button
                    onClick={() => setSelected(workflow.workflow_id)}
                    className={`w-full px-4 py-3 text-left transition-colors ${
                      selected === workflow.workflow_id
                        ? 'bg-white/[0.05]'
                        : 'hover:bg-white/[0.02]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Mono className="truncate">{workflow.workflow_id}</Mono>
                      <Badge tone={WORKFLOW_STATUS_TONE[workflow.status ?? ''] ?? 'muted'}>
                        {workflow.status ?? '—'}
                      </Badge>
                    </div>
                    <p className="mt-1 text-[10px] text-steel-600">
                      {workflow.node ?? '—'} · step {workflow.step ?? 0} ·{' '}
                      {relativeTime(workflow.updated_at)}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* detail */}
        <div className="space-y-4">
          {!selected ? (
            <Card>
              <EmptyState
                icon={<IconDirector />}
                title="Select a workflow"
                description="Workflow state is checkpointed in the database and survives restarts."
              />
            </Card>
          ) : detail.loading ? (
            <Card>
              <div className="space-y-3 p-5">
                <Skeleton className="h-6 w-1/3" />
                <Skeleton className="h-24 w-full" />
                <Skeleton className="h-32 w-full" />
              </div>
            </Card>
          ) : detail.error ? (
            <Card>
              <div className="p-5">
                <ErrorState message={detail.error.message} onRetry={detail.reload} />
              </div>
            </Card>
          ) : detail.data ? (
            <WorkflowDetail
              workflow={detail.data}
              canApprove={canApprove}
              onChanged={() => {
                detail.reload()
                workflows.reload()
              }}
              notify={notify}
            />
          ) : null}

          <Card>
            <CardHeader
              title="Graph topology"
              subtitle="Static shape of the compiled state machine."
            />
            <div className="px-5 py-4">
              {topology.data ? (
                <div className="space-y-3">
                  <div className="flex flex-wrap gap-1.5">
                    {topology.data.nodes.map((node) => (
                      <span
                        key={node}
                        className="rounded-md border border-white/[0.08] bg-white/[0.02] px-2 py-0.5 hq-mono text-steel-400"
                      >
                        {node}
                      </span>
                    ))}
                  </div>
                  <div className="space-y-1">
                    {topology.data.edges.map(([from, to], index) => (
                      <p key={index} className="hq-mono">
                        {from} → {to}
                      </p>
                    ))}
                  </div>
                  <p className="text-[11px] text-steel-600">
                    checkpointer: {topology.data.checkpointer} · interrupts:{' '}
                    {topology.data.interrupts.join(', ')}
                  </p>
                </div>
              ) : (
                <Skeleton className="h-24 w-full" />
              )}
            </div>
          </Card>
        </div>
      </div>

      <StartWorkflowModal
        open={startOpen}
        onClose={() => setStartOpen(false)}
        onStarted={(id) => {
          setSelected(id)
          workflows.reload()
          detail.reload()
        }}
        notify={notify}
      />
    </div>
  )
}

function WorkflowDetail({
  workflow,
  canApprove,
  onChanged,
  notify,
}: {
  workflow: WorkflowStatus
  canApprove: boolean
  onChanged: () => void
  notify: (message: string, tone?: 'neutral' | 'ok' | 'danger') => void
}) {
  const [decisionOpen, setDecisionOpen] = useState(false)
  const [action, setAction] = useState<'approve' | 'request_changes' | 'reject'>('approve')
  const [reason, setReason] = useState('')

  const decide = useMutation((payload: { action: string; reason: string }) =>
    directorApi.decide(workflow.workflow_id, payload),
  )

  async function submitDecision() {
    const result = await decide.run({ action, reason })
    if (result) {
      notify(`Decision recorded: ${action.replace('_', ' ')}.`, 'ok')
      setDecisionOpen(false)
      setReason('')
      onChanged()
    }
  }

  const items = workflow.approval_request?.items ?? []

  return (
    <>
      <Card>
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              <Mono>{workflow.workflow_id}</Mono>
              <Badge tone={WORKFLOW_STATUS_TONE[workflow.status ?? ''] ?? 'muted'} dot>
                {workflow.status ?? 'unknown'}
              </Badge>
              {workflow.awaiting_human ? <Badge tone="warn">awaiting founder</Badge> : null}
            </span>
          }
          subtitle={workflow.summary ?? undefined}
          action={
            workflow.awaiting_human ? (
              <Button
                variant="primary"
                size="sm"
                disabled={!canApprove}
                title={canApprove ? undefined : 'Only the founder can decide'}
                onClick={() => setDecisionOpen(true)}
              >
                <IconLock size={13} /> Record decision
              </Button>
            ) : undefined
          }
        />
        <div className="grid gap-5 px-5 py-4 lg:grid-cols-2">
          <div>
            <SectionTitle>Plan</SectionTitle>
            <PlanView plan={workflow.plan} />
          </div>
          <div>
            <SectionTitle>Quality gate</SectionTitle>
            <div className="mb-2 flex items-center gap-2">
              <VerdictBadge verdict={workflow.qa_verdict} />
              <Mono>{workflow.checkpoint_count} checkpoints</Mono>
              <Mono>iteration {workflow.iteration ?? 0}</Mono>
            </div>
            <div className="max-h-[280px] overflow-y-auto pr-1">
              <QAFindings findings={workflow.qa_findings} />
            </div>
          </div>
        </div>

        {workflow.approval_request ? (
          <div className="border-t border-white/[0.06] px-5 py-4">
            <SectionTitle>Approval packet</SectionTitle>
            <p className="mb-3 text-[11px] leading-relaxed text-steel-500">
              {workflow.approval_request.note}
            </p>
            <div className="space-y-2">
              {items.map((item) => (
                <Link
                  key={item.content_item_id}
                  to={`/content/${item.content_item_id}`}
                  className="flex items-center justify-between gap-3 rounded-lg border border-white/[0.07] bg-white/[0.02] px-3 py-2.5 transition-colors hover:border-white/[0.14]"
                >
                  <div className="min-w-0">
                    <p className="truncate text-xs text-steel-300">{item.title}</p>
                    <p className="mt-0.5 hq-mono">
                      {item.platform} · v{item.version_count} · {item.status}
                    </p>
                  </div>
                  <Badge tone="warn">review</Badge>
                </Link>
              ))}
              {items.length === 0 ? (
                <p className="text-[11px] text-steel-600">
                  No content items were produced by this workflow.
                </p>
              ) : null}
            </div>
          </div>
        ) : null}

        {workflow.approval_decision ? (
          <div className="border-t border-white/[0.06] px-5 py-3">
            <div className="flex items-center gap-2 text-[11px] text-steel-500">
              <Badge tone="ok">decided</Badge>
              <span>
                {String((workflow.approval_decision as { action?: string }).action ?? '')}
                {reason ? ` · ${reason}` : ''}
              </span>
            </div>
          </div>
        ) : null}

        {workflow.errors && workflow.errors.length > 0 ? (
          <div className="border-t border-white/[0.06] px-5 py-3">
            <SectionTitle>Errors</SectionTitle>
            <ul className="space-y-1">
              {workflow.errors.map((error, index) => (
                <li key={index} className="hq-mono text-signal-danger/90">
                  {String(error.code ?? 'error')}: {String(error.message ?? '')}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {workflow.trace && workflow.trace.length > 0 ? (
          <details className="border-t border-white/[0.06] px-5 py-3">
            <summary className="cursor-pointer text-[11px] text-steel-500 hover:text-steel-400">
              Execution trace ({workflow.trace.length} events)
            </summary>
            <ul className="mt-2 max-h-52 space-y-0.5 overflow-y-auto">
              {workflow.trace.map((entry, index) => (
                <li key={index} className="hq-mono">
                  {String(entry.entry ?? entry.type ?? 'event')}
                  {entry.task ? ` · ${String(entry.task)}` : ''}
                  {entry.agent ? ` · ${String(entry.agent)}` : ''}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </Card>

      <Modal
        open={decisionOpen}
        onClose={() => setDecisionOpen(false)}
        title="Record founder decision"
        description="This resumes the paused workflow. Approving marks the exact current content versions as approved — it does not publish them."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDecisionOpen(false)}>
              Cancel
            </Button>
            <Button
              variant={action === 'reject' ? 'danger' : 'primary'}
              loading={decide.pending}
              onClick={submitDecision}
            >
              Confirm {action.replace('_', ' ')}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            {(['approve', 'request_changes', 'reject'] as const).map((option) => (
              <button
                key={option}
                onClick={() => setAction(option)}
                className={`rounded-lg border px-3 py-2 text-[11px] font-medium transition-colors ${
                  action === option
                    ? 'border-violet-500/50 bg-violet-500/10 text-violet-200'
                    : 'border-white/[0.08] text-steel-400 hover:border-white/[0.16]'
                }`}
              >
                {option.replace('_', ' ')}
              </button>
            ))}
          </div>
          <Textarea
            label="Reason (audited)"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why this decision was made…"
          />
          {decide.error ? <Banner tone="danger">{decide.error.message}</Banner> : null}
        </div>
      </Modal>
    </>
  )
}

function StartWorkflowModal({
  open,
  onClose,
  onStarted,
  notify,
}: {
  open: boolean
  onClose: () => void
  onStarted: (id: string) => void
  notify: (message: string, tone?: 'neutral' | 'ok' | 'danger') => void
}) {
  const [objective, setObjective] = useState('')
  const [platforms, setPlatforms] = useState<Platform[]>(['linkedin'])
  const [error, setError] = useState<string | null>(null)

  const start = useMutation(async () => {
    const result = await directorApi.start(
      { objective, platforms },
      newIdempotencyKey('wf'),
    )
    return result
  })

  async function submit() {
    setError(null)
    if (objective.trim().length < 3) {
      setError('Describe the objective in at least 3 characters.')
      return
    }
    if (platforms.length === 0) {
      setError('Select at least one platform.')
      return
    }
    const result = await start.run(undefined as never)
    if (result) {
      notify('Workflow started. Review the plan, then record your decision.', 'ok')
      setObjective('')
      onStarted(result.workflow_id)
      onClose()
    } else if (start.error instanceof ApiError) {
      setError(start.error.message)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Start a marketing workflow"
      description="The director will research, plan, draft content for each platform, run brand QA, and then wait for your approval."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={start.pending} onClick={submit}>
            Run workflow
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Textarea
          label="Objective"
          value={objective}
          onChange={(event) => setObjective(event.target.value)}
          placeholder="e.g. Introduce Crusor AI to developers who are evaluating AI-assisted IDEs"
          hint="Be specific. The director uses this to scope research and planning."
        />
        <div>
          <span className="hq-label mb-1.5 block">Platforms</span>
          <div className="grid grid-cols-3 gap-2">
            {PLATFORMS.map((platform) => {
              const active = platforms.includes(platform)
              return (
                <button
                  key={platform}
                  onClick={() =>
                    setPlatforms((current) =>
                      current.includes(platform)
                        ? current.filter((p) => p !== platform)
                        : [...current, platform],
                    )
                  }
                  className={`rounded-lg border px-2 py-1.5 text-[11px] capitalize transition-colors ${
                    active
                      ? 'border-violet-500/50 bg-violet-500/10 text-violet-200'
                      : 'border-white/[0.08] text-steel-400 hover:border-white/[0.16]'
                  }`}
                >
                  {platform}
                </button>
              )
            })}
          </div>
        </div>
        {error ? <Banner tone="danger">{error}</Banner> : null}
        <p className="text-[11px] leading-relaxed text-steel-600">
          No LLM provider is required. Without one, agents run their deterministic implementations
          and report <Mono>generation_mode: deterministic</Mono>.
        </p>
        <Input label="Brand" value="Active brand profile" readOnly disabled />
      </div>
    </Modal>
  )
}

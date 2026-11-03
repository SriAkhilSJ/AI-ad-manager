import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi, useMutation } from '../../hooks/useApi'
import { useAuth } from '../../hooks/useAuth'
import { useToast } from '../../hooks/useToast'
import { creativeStudioApi } from '../../lib/api'
import type { CreativeJob } from '../../lib/types'
import { formatDateTime } from '../../lib/format'
import { ErrorState } from '../../components/layout/PageHeader'
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Mono,
  Select,
  Skeleton,
} from '../../components/ui'

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'timed_out'])

function statusTone(status: string): 'ok' | 'warn' | 'danger' | 'muted' | 'violet' {
  if (status === 'succeeded') return 'ok'
  if (status === 'failed' || status === 'timed_out') return 'danger'
  if (status === 'cancelled') return 'muted'
  if (status === 'running' || status === 'submitted') return 'violet'
  return 'warn'
}

/**
 * Generation centre.
 *
 * Submitting is idempotent server-side: the same brief and parameters return the
 * existing job instead of creating a second one. Cancelling a job that a provider
 * cannot confirm is *not* the same as proving it did not run, so an ambiguous
 * timeout never offers a one-click retry.
 */
export function CreativeGeneration() {
  const { canEdit } = useAuth()
  const { notify } = useToast()
  const [briefId, setBriefId] = useState('')
  const [scenario, setScenario] = useState('')
  const [busyJob, setBusyJob] = useState<string | null>(null)

  const briefs = useApi(() => creativeStudioApi.briefs({ limit: 50 }))
  const jobs = useApi(() => creativeStudioApi.jobs({ limit: 50 }))
  const capabilities = useApi(() => creativeStudioApi.capabilities())

  const submit = useMutation(async () => {
    if (!briefId) throw new Error('Choose a brief first.')
    const parameters: Record<string, unknown> = {}
    if (scenario) parameters.mock_simulate = scenario
    return creativeStudioApi.submitJob({ brief_id: briefId, parameters })
  })

  async function doSubmit() {
    const result = await submit.run(undefined as never)
    if (result) {
      notify(
        result.status === 'succeeded'
          ? `Mock job ${result.external_job_id.slice(0, 22)}… produced an asset.`
          : `Job ${result.status}.`,
        result.status === 'succeeded' ? 'ok' : 'neutral',
      )
      jobs.reload()
    }
  }

  async function act(job: CreativeJob, action: 'poll' | 'retry' | 'cancel') {
    setBusyJob(job.id)
    try {
      const updated =
        action === 'poll'
          ? await creativeStudioApi.pollJob(job.id)
          : action === 'retry'
            ? await creativeStudioApi.retryJob(job.id)
            : await creativeStudioApi.cancelJob(job.id)
      notify(`Job is now ${updated.status}.`, updated.status === 'succeeded' ? 'ok' : 'neutral')
      jobs.reload()
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : 'Action failed.', 'danger')
    } finally {
      setBusyJob(null)
    }
  }

  const briefOptions = briefs.data ?? []
  const isMock = capabilities.data?.is_mock ?? false
  const selectedBrief = useMemo(
    () => briefOptions.find((brief) => brief.id === briefId) ?? null,
    [briefOptions, briefId],
  )

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Submit a generation job"
          subtitle="One brief in, one job out. The same brief and parameters reuse the existing job."
        />
        <div className="flex flex-wrap items-end gap-3 px-5 py-4">
          <label className="block min-w-[260px] flex-1">
            <span className="hq-label">Brief</span>
            <Select
              className="mt-1 w-full"
              value={briefId}
              onChange={(event) => setBriefId(event.target.value)}
            >
              <option value="">Select a brief…</option>
              {briefOptions.map((brief) => (
                <option key={brief.id} value={brief.id}>
                  v{brief.version_number} · {brief.platform} · {brief.asset_type} ·{' '}
                  {brief.id.slice(0, 8)}
                </option>
              ))}
            </Select>
          </label>
          {isMock ? (
            <label className="block min-w-[190px]">
              <span className="hq-label">Mock scenario</span>
              <Select
                className="mt-1 w-full"
                value={scenario}
                onChange={(event) => setScenario(event.target.value)}
              >
                <option value="">succeed</option>
                <option value="fail">fail immediately</option>
                <option value="timeout">stay running</option>
                <option value="ambiguous_timeout">ambiguous timeout</option>
              </Select>
            </label>
          ) : null}
          <Button
            variant="primary"
            loading={submit.pending}
            disabled={!canEdit || !briefId}
            onClick={doSubmit}
          >
            Submit job
          </Button>
        </div>
        {selectedBrief ? (
          <div className="border-t border-white/[0.06] px-5 py-3 text-[11px] leading-relaxed text-steel-500">
            {selectedBrief.aspect_ratio} · {selectedBrief.resolution}
            {selectedBrief.duration_seconds ? ` · ${selectedBrief.duration_seconds}s` : ''} ·{' '}
            <Mono>{selectedBrief.image_prompt.slice(0, 120)}…</Mono>
          </div>
        ) : null}
        {submit.error ? (
          <div className="px-5 pb-4">
            <Banner tone="danger">{submit.error.message}</Banner>
          </div>
        ) : null}
        {isMock ? (
          <div className="px-5 pb-4">
            <Banner tone="warn">
              The mock provider is deterministic and free. Scenario controls exist only to exercise
              failure, timeout and cancellation handling — they are not a model selector.
            </Banner>
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title="Job history"
          subtitle={jobs.data ? `${jobs.data.length} job(s)` : undefined}
          action={
            <Button size="sm" variant="ghost" onClick={jobs.reload}>
              Refresh
            </Button>
          }
        />
        {jobs.error ? (
          <div className="p-4">
            <ErrorState message={jobs.error.message} onRetry={jobs.reload} />
          </div>
        ) : jobs.loading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : (jobs.data ?? []).length === 0 ? (
          <EmptyState
            title="No jobs yet"
            description="Submit a brief above. History, errors and audit events appear here."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="hq-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Provider</th>
                  <th>Attempts</th>
                  <th>Asset</th>
                  <th>Created</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(jobs.data ?? []).map((job) => (
                  <tr key={job.id}>
                    <td className="max-w-[260px]">
                      <div className="truncate text-xs text-steel-300">
                        {job.job_type} · brief v{job.brief_version}
                      </div>
                      <div className="mt-0.5 truncate">
                        <Mono>{job.external_job_id}</Mono>
                      </div>
                      {job.error_message ? (
                        <div className="mt-1 text-[11px] text-signal-danger">
                          {job.error_code}: {job.error_message}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <Badge tone={statusTone(job.status)}>{job.status.replace(/_/g, ' ')}</Badge>
                      {job.ambiguous ? (
                        <span className="ml-1 text-[10px] text-signal-warn">ambiguous</span>
                      ) : null}
                      {job.is_mock ? (
                        <span className="ml-1 text-[10px] text-steel-600">mock</span>
                      ) : null}
                    </td>
                    <td className="text-steel-500">{job.provider}</td>
                    <td className="hq-mono text-steel-500">{job.attempts}</td>
                    <td>
                      {job.asset_id ? (
                        <Link
                          to={`/creative/assets/${job.asset_id}`}
                          className="text-xs text-violet-300 hover:text-violet-200"
                        >
                          open
                        </Link>
                      ) : (
                        <span className="text-steel-600">—</span>
                      )}
                    </td>
                    <td className="text-steel-500">{formatDateTime(job.created_at)}</td>
                    <td className="text-right">
                      <div className="flex justify-end gap-1">
                        {!TERMINAL.has(job.status) ? (
                          <>
                            <Button
                              size="sm"
                              variant="ghost"
                              loading={busyJob === job.id}
                              disabled={!canEdit}
                              onClick={() => act(job, 'poll')}
                            >
                              Poll
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={!canEdit || busyJob === job.id}
                              onClick={() => act(job, 'cancel')}
                            >
                              Cancel
                            </Button>
                          </>
                        ) : null}
                        {job.status === 'failed' && job.error_retryable ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            loading={busyJob === job.id}
                            disabled={!canEdit}
                            onClick={() => act(job, 'retry')}
                          >
                            Retry
                          </Button>
                        ) : null}
                        {job.status === 'timed_out' ? (
                          <Button
                            size="sm"
                            variant="danger"
                            loading={busyJob === job.id}
                            disabled={!canEdit}
                            onClick={() => {
                              if (
                                window.confirm(
                                  'The provider could not confirm whether this job ran. Resubmitting may duplicate a paid generation. Continue?',
                                )
                              ) {
                                setBusyJob(job.id)
                                creativeStudioApi
                                  .retryJob(job.id, true)
                                  .then(() => jobs.reload())
                                  .catch((cause: unknown) =>
                                    notify(
                                      cause instanceof Error ? cause.message : 'Retry failed.',
                                      'danger',
                                    ),
                                  )
                                  .finally(() => setBusyJob(null))
                              }
                            }}
                          >
                            Retry anyway
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

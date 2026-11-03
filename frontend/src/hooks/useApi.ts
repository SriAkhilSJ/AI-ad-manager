/**
 * Data-fetching hooks with no fabricated fallbacks.
 *
 * `useApi` returns `{ data, error, loading }` where `data` is `null` until a real
 * response arrives. Pages must therefore render a skeleton or an empty state —
 * they cannot accidentally display invented data.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../lib/api'

export interface ApiState<T> {
  data: T | null
  error: ApiError | null
  loading: boolean
  reload: () => void
}

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  options: { enabled?: boolean } = {},
): ApiState<T> {
  const enabled = options.enabled ?? true
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [tick, setTick] = useState(0)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reload = useCallback(() => setTick((value) => value + 1), [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    let cancelled = false
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    fetcherRef
      .current()
      .then((result) => {
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      })
      .catch((cause: unknown) => {
        if (cancelled) return
        if ((cause as Error)?.name === 'AbortError') return
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, 'unknown_error', (cause as Error)?.message ?? 'Request failed.'),
        )
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, tick, ...deps])

  return { data, error, loading, reload }
}

/** Mutation helper with pending/error state and a typed result. */
export function useMutation<TArgs, TResult>(
  action: (args: TArgs) => Promise<TResult>,
): {
  run: (args: TArgs) => Promise<TResult | null>
  pending: boolean
  error: ApiError | null
  reset: () => void
} {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const run = useCallback(
    async (args: TArgs) => {
      setPending(true)
      setError(null)
      try {
        const result = await action(args)
        return result
      } catch (cause) {
        if (mounted.current) {
          setError(
            cause instanceof ApiError
              ? cause
              : new ApiError(0, 'unknown_error', (cause as Error)?.message ?? 'Request failed.'),
          )
        }
        return null
      } finally {
        if (mounted.current) setPending(false)
      }
    },
    [action],
  )

  return { run, pending, error, reset: () => setError(null) }
}

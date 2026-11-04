import { useEffect, useState } from 'react'
import { creativeStudioApi } from '../../lib/api'

/**
 * Fetch an asset's bytes as an object URL for <img> previews.
 *
 * The download endpoint needs the bearer token, so an <img src> straight at the
 * API would 401. The blob URL is revoked when the component unmounts or the
 * asset changes, so previews cannot leak memory across a long session.
 */
export function useAssetPreview(assetId: string | null | undefined) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    setUrl(null)
    setError(null)
    if (!assetId) return

    creativeStudioApi
      .bytes(assetId)
      .then((blob) => {
        if (revoked) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((cause: unknown) => {
        if (revoked) return
        setError(cause instanceof Error ? cause.message : 'Preview unavailable.')
      })

    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [assetId])

  return { url, error }
}

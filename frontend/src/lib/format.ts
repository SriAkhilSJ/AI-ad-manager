import type { ContentStatus, Platform } from './types'

export function formatDateTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDate(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleDateString(undefined, { month: 'short', day: '2-digit', year: 'numeric' })
}

export function relativeTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const diff = Date.now() - date.getTime()
  const minutes = Math.round(diff / 60000)
  if (Math.abs(minutes) < 1) return 'just now'
  if (Math.abs(minutes) < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (Math.abs(days) < 30) return `${days}d ago`
  return formatDate(value)
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return new Intl.NumberFormat().format(value)
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value === 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export const STATUS_LABEL: Record<ContentStatus, string> = {
  DRAFT: 'Draft',
  IN_REVIEW: 'In review',
  APPROVED: 'Approved',
  SCHEDULED: 'Scheduled',
  PUBLISHING: 'Publishing',
  PUBLISHED: 'Published',
  REJECTED: 'Rejected',
  CHANGES_REQUESTED: 'Changes requested',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

export const PLATFORM_LABEL: Record<Platform, string> = {
  instagram: 'Instagram',
  linkedin: 'LinkedIn',
  youtube: 'YouTube',
  x: 'X',
  facebook: 'Facebook',
  reddit: 'Reddit',
}

export function platformLabel(platform: string): string {
  return PLATFORM_LABEL[platform as Platform] ?? platform
}

export function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim()
}

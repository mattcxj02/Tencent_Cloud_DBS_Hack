// Time formatting. The gateway stores and signs UTC; the customer sees Singapore time.

export function parseIso(value: string | null | undefined): number | null {
  if (!value) return null
  const t = Date.parse(value)
  return Number.isNaN(t) ? null : t
}

const TIME_FMT = new Intl.DateTimeFormat('en-SG', {
  timeZone: 'Asia/Singapore',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

const DATE_TIME_FMT = new Intl.DateTimeFormat('en-SG', {
  timeZone: 'Asia/Singapore',
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatTime(value: string | null | undefined): string {
  const t = parseIso(value)
  return t === null ? (value ?? '') : `${TIME_FMT.format(t)} SGT`
}

export function formatDateTime(value: string | null | undefined): string {
  const t = parseIso(value)
  return t === null ? (value ?? '') : `${DATE_TIME_FMT.format(t)} SGT`
}

/** 95 → "1:35"; used for the approval countdown. */
export function formatSeconds(total: number): string {
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** Seconds → "11 h 52 min" (cooling periods). */
export function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  if (h > 0) return `${h} h ${m} min`
  if (m > 0) return `${m} min`
  return `${totalSeconds} s`
}

/** Shorten a hash for display; the full value stays available in technical details. */
export function shortHash(value: string | null | undefined, n = 12): string {
  if (!value) return '—'
  return value.length > n ? `${value.slice(0, n)}…` : value
}

/** Only http(s) links are ever rendered as anchors. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : null
  } catch {
    return null
  }
}

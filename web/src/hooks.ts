import { useEffect, useRef, useState } from 'react'
import { parseIso } from './format'

/**
 * Seconds left until `expiresAt` (server UTC). `skewMs` is server-minus-client clock
 * offset measured when the envelope arrived, so a wrong laptop clock does not lie.
 */
export function useCountdown(expiresAt: string | null | undefined, skewMs = 0): number | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!expiresAt) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [expiresAt])
  const t = parseIso(expiresAt)
  if (t === null) return null
  return Math.max(0, Math.ceil((t - (now + skewMs)) / 1000))
}

/** Move keyboard/screen-reader focus to an element when `when` becomes true. */
export function useFocusWhen<T extends HTMLElement>(when: unknown) {
  const ref = useRef<T>(null)
  useEffect(() => {
    if (when) ref.current?.focus()
  }, [when])
  return ref
}

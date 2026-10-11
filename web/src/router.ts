// Minimal hash routing: #/chat, #/mandate, #/evidence[/<conversation_id>], #/baseline, #/signin.
import { useEffect, useState } from 'react'

export type RouteName = 'chat' | 'mandate' | 'evidence' | 'baseline' | 'signin'

export interface Route {
  name: RouteName
  param: string | null
}

const NAMES: RouteName[] = ['chat', 'mandate', 'evidence', 'baseline', 'signin']

export function parseHash(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const name = (NAMES as string[]).includes(parts[0] ?? '') ? (parts[0] as RouteName) : 'chat'
  const param = parts[1] ? decodeURIComponent(parts[1]) : null
  return { name, param }
}

export function hrefFor(name: RouteName, param?: string | null): string {
  return param ? `#/${name}/${encodeURIComponent(param)}` : `#/${name}`
}

export function navigate(name: RouteName, param?: string | null): void {
  window.location.hash = hrefFor(name, param)
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

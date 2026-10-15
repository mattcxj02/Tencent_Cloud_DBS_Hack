// Persistent header: simulated-bank label, product name, signed-in user, account balances,
// card outstanding balances and nav.
// Gateway health is shown in the footer (ServiceStatus), away from the customer's task.
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Health } from '../api'
import { formatMoney } from '../money'
import { hrefFor } from '../router'
import type { Route, RouteName } from '../router'
import { useSession } from '../session'

// Mandate settings (#/mandate) and the usability baseline (#/baseline) stay reachable by
// URL; they are not part of the customer's everyday navigation. Limits are enforced either way.
const NAV: { name: RouteName; label: string; auth: boolean }[] = [
  { name: 'chat', label: 'Chat', auth: true },
  { name: 'payees', label: 'Payees', auth: true },
  { name: 'evidence', label: 'Evidence', auth: true },
]

function useHealth(): { text: string; ok: boolean | null; detail: Health | null } {
  const [state, setState] = useState<{ text: string; ok: boolean | null; detail: Health | null }>({
    text: 'checking…',
    ok: null,
    detail: null,
  })
  useEffect(() => {
    let alive = true
    const check = async () => {
      try {
        const h = await api.health()
        if (alive) setState({ text: h.status === 'ok' ? 'online' : h.status, ok: h.status === 'ok', detail: h })
      } catch {
        if (alive) setState({ text: 'unreachable', ok: false, detail: null })
      }
    }
    void check()
    const id = window.setInterval(check, 30_000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])
  return state
}

/** Small footer line: is the (simulated) bank reachable, and is tracing configured. */
export function ServiceStatus() {
  const health = useHealth()
  return (
    <p className="text-sm" title={health.detail ? `ASR: ${health.detail.trtc ?? '?'} · tracing: ${health.detail.langfuse ?? '?'}` : undefined}>
      Bank service:{' '}
      <strong>
        <span aria-hidden="true">{health.ok === null ? '…' : health.ok ? '● ' : '✕ '}</span>
        {health.text}
      </strong>
      {health.detail?.langfuse && <> · tracing {health.detail.langfuse === 'configured' ? 'on' : 'not configured'}</>}
    </p>
  )
}

export function Header({ route }: { route: Route }) {
  const { status, me, accounts, accountsError, cards, signOut } = useSession()
  const signedIn = status === 'signed-in'

  return (
    <header className="bg-slate-900 text-white">
      <p className="bg-amber-300 px-4 py-1 text-center text-sm font-semibold text-slate-950">
        Simulated bank · hackathon prototype · no real money moves
      </p>
      <div className="mx-auto max-w-2xl space-y-2 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-lg">
            <span className="text-xl font-bold">SaySo</span>
            <span className="ml-2 hidden text-sm text-slate-300 sm:inline">Nothing moves without your say-so.</span>
          </p>
          {signedIn && (
            <div className="flex items-center gap-2 text-sm">
              <span>
                Signed in as <strong>{me?.display_name ?? me?.user_id}</strong>
              </span>
              <button
                type="button"
                onClick={() => void signOut()}
                className="min-h-[44px] rounded-lg border border-white px-3 font-semibold hover:bg-slate-800"
              >
                Sign out
              </button>
            </div>
          )}
        </div>

        {signedIn && (
          <div className="text-sm">
            <p className="sr-only">Balances</p>
            {accounts ? (
              <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label="Account and card balances">
                {accounts.map((a) => (
                  <li key={a.account_id}>
                    {a.display}: <strong className="tabular-nums">{formatMoney(a.balance)}</strong>
                  </li>
                ))}
                {cards?.map((c) => (
                  <li key={c.card_id}>
                    {c.display} outstanding: <strong className="tabular-nums">{formatMoney(c.outstanding)}</strong>
                  </li>
                ))}
              </ul>
            ) : (
              <p>{accountsError ?? 'Loading balances…'}</p>
            )}
          </div>
        )}

        <nav aria-label="Main">
          <ul className="flex flex-wrap gap-1">
            {NAV.filter((n) => signedIn || !n.auth).map((n) => {
              const current = route.name === n.name
              return (
                <li key={n.name}>
                  <a
                    href={hrefFor(n.name)}
                    aria-current={current ? 'page' : undefined}
                    className={`inline-flex min-h-[44px] items-center rounded-lg px-3 text-base ${
                      current ? 'bg-white font-bold text-slate-950 underline underline-offset-4' : 'text-white hover:bg-slate-800'
                    }`}
                  >
                    {n.label}
                  </a>
                </li>
              )
            })}
            {!signedIn && (
              <li>
                <a
                  href={hrefFor('signin')}
                  aria-current={route.name === 'signin' ? 'page' : undefined}
                  className="inline-flex min-h-[44px] items-center rounded-lg px-3 text-base text-white hover:bg-slate-800"
                >
                  Sign in
                </a>
              </li>
            )}
          </ul>
        </nav>
      </div>
    </header>
  )
}

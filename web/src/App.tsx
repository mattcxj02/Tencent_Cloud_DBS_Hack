// App shell: providers, header, hash routes and the skip link.
import { useEffect, useRef } from 'react'
import type { MouseEvent } from 'react'
import { SignInPage } from './components/Auth'
import { BaselinePage } from './components/BaselinePage'
import { ChatPage } from './components/Chat'
import { EvidencePage } from './components/EvidencePage'
import { setFacilitatorEnabled, useFacilitatorEnabled } from './components/FacilitatorPanel'
import { Header } from './components/Header'
import { MandatePage } from './components/MandatePage'
import { PayeesPage } from './components/PayeesPage'
import { navigate, useHashRoute } from './router'
import type { RouteName } from './router'
import { AnnouncerProvider, SessionProvider, useSession } from './session'

const TITLE: Record<RouteName, string> = {
  chat: 'Payments assistant',
  payees: 'Payees and nicknames',
  mandate: 'Mandate settings',
  evidence: 'Evidence trail',
  baseline: 'Baseline prototype',
  signin: 'Sign in',
}

function Shell() {
  const route = useHashRoute()
  const session = useSession()
  const mainRef = useRef<HTMLElement>(null)
  const facilitator = useFacilitatorEnabled()

  useEffect(() => {
    document.title = `${TITLE[route.name]} — DCTA (simulated bank)`
  }, [route.name])

  // Signed in on the sign-in route: go to chat.
  useEffect(() => {
    if (session.status === 'signed-in' && route.name === 'signin') navigate('chat')
  }, [session.status, route.name])

  const skip = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault() // hash routing: do not change the route
    mainRef.current?.focus()
  }

  let page
  if (route.name === 'baseline') page = <BaselinePage />
  else if (session.status === 'loading') page = <p className="text-base">Loading…</p>
  else if (session.status !== 'signed-in') page = <SignInPage />
  else if (route.name === 'payees') page = <PayeesPage />
  else if (route.name === 'mandate') page = <MandatePage />
  else if (route.name === 'evidence') page = <EvidencePage key={route.param ?? ''} conversationId={route.param} />
  else page = <ChatPage />

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      <a
        href="#main"
        onClick={skip}
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:font-semibold focus:text-slate-950"
      >
        Skip to main content
      </a>
      <Header route={route} />
      <main id="main" ref={mainRef} tabIndex={-1} className="mx-auto max-w-2xl px-4 py-4">
        {page}
      </main>
      <footer className="mx-auto max-w-2xl px-4 pb-6 text-sm text-slate-700">
        <p>
          Prototype on a simulated bank: accounts, PayNow, SGX quotes, KYC, sanctions and the phone code are all mock
          services. Passkey approval is real WebAuthn on this device.
        </p>
        <button
          type="button"
          onClick={() => setFacilitatorEnabled(!facilitator)}
          className="mt-2 min-h-[44px] underline underline-offset-2"
          aria-pressed={facilitator}
        >
          Facilitator timer on chat: {facilitator ? 'on' : 'off'}
        </button>
      </footer>
    </div>
  )
}

export function App() {
  return (
    <AnnouncerProvider>
      <SessionProvider>
        <Shell />
      </SessionProvider>
    </AnnouncerProvider>
  )
}

// Signed-in user, the passkey credential to bind approvals to, balances, and a polite
// screen-reader announcer shared by every page.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api, ApiError, setUnauthorizedHandler } from './api'
import type { Account, Me } from './api'
import { describeCode } from './messages'

const CREDENTIAL_KEY = 'dcta.credential_id'
const USER_KEY = 'dcta.user_id'

function readStore(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStore(key: string, value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, value)
  } catch {
    // Storage blocked: the app still works, it just forgets the credential on reload.
  }
}

export interface Session {
  status: 'loading' | 'signed-out' | 'signed-in'
  me: Me | null
  /** The passkey that signed in on this device; approvals are bound to it. */
  credentialId: string | null
  accounts: Account[] | null
  accountsError: string | null
  notice: string | null
  refreshAccounts: () => Promise<void>
  completeSignIn: (userId: string, credentialId: string) => Promise<void>
  signOut: () => Promise<void>
}

const SessionContext = createContext<Session | null>(null)

export function useSession(): Session {
  const s = useContext(SessionContext)
  if (!s) throw new Error('useSession outside SessionProvider')
  return s
}

function pickCredential(me: Me, preferred: string | null): string | null {
  if (preferred && me.credentials.some((c) => c.credential_id === preferred)) return preferred
  const newest = [...me.credentials].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
  return newest?.credential_id ?? preferred
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Session['status']>('loading')
  const [me, setMe] = useState<Me | null>(null)
  const [credentialId, setCredentialId] = useState<string | null>(null)
  const [accounts, setAccounts] = useState<Account[] | null>(null)
  const [accountsError, setAccountsError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refreshAccounts = useCallback(async () => {
    try {
      setAccounts(await api.accounts())
      setAccountsError(null)
    } catch (e) {
      setAccountsError(e instanceof ApiError ? describeCode(e.code) : 'Balances unavailable.')
    }
  }, [])

  const loadMe = useCallback(
    async (fallbackUserId: string | null, fallbackCredential: string | null) => {
      try {
        const who = await api.me()
        setMe(who)
        const cred = pickCredential(who, fallbackCredential)
        setCredentialId(cred)
        writeStore(CREDENTIAL_KEY, cred)
        writeStore(USER_KEY, who.user_id)
        setStatus('signed-in')
        void refreshAccounts()
      } catch (e) {
        if (e instanceof ApiError && e.status !== 401 && fallbackUserId && fallbackCredential) {
          // /api/auth/me unavailable: continue with what the sign-in itself returned.
          setMe({ user_id: fallbackUserId, display_name: fallbackUserId, credentials: [] })
          setCredentialId(fallbackCredential)
          setStatus('signed-in')
          void refreshAccounts()
          return
        }
        setMe(null)
        setStatus('signed-out')
      }
    },
    [refreshAccounts],
  )

  useEffect(() => {
    void loadMe(readStore(USER_KEY), readStore(CREDENTIAL_KEY))
  }, [loadMe])

  useEffect(() => {
    // Only authenticated routes report 401 here (sign-in routes and the /me probe are quiet).
    setUnauthorizedHandler(() => {
      setNotice(describeCode('SESSION_REQUIRED'))
      setStatus('signed-out')
      setMe(null)
    })
    return () => setUnauthorizedHandler(null)
  }, [])

  const completeSignIn = useCallback(
    async (userId: string, cred: string) => {
      setNotice(null)
      writeStore(USER_KEY, userId)
      writeStore(CREDENTIAL_KEY, cred)
      await loadMe(userId, cred)
    },
    [loadMe],
  )

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } catch {
      // The cookie may already be gone; signing out locally is still correct.
    }
    writeStore(USER_KEY, null)
    writeStore(CREDENTIAL_KEY, null)
    try {
      window.sessionStorage.removeItem('dcta.conversation_id')
    } catch {
      // ignore
    }
    setMe(null)
    setCredentialId(null)
    setAccounts(null)
    setStatus('signed-out')
  }, [])

  const value = useMemo<Session>(
    () => ({
      status,
      me,
      credentialId,
      accounts,
      accountsError,
      notice,
      refreshAccounts,
      completeSignIn,
      signOut,
    }),
    [status, me, credentialId, accounts, accountsError, notice, refreshAccounts, completeSignIn, signOut],
  )
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

// ── Screen-reader announcements ──────────────────────────────────────

const AnnounceContext = createContext<(message: string) => void>(() => {})

export function useAnnounce(): (message: string) => void {
  return useContext(AnnounceContext)
}

/** One polite live region for status updates ("Draft ready", "Paid", countdown warnings). */
export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState('')
  const timer = useRef<number | undefined>(undefined)
  const announce = useCallback((text: string) => {
    // Clear first so repeating the same sentence is announced again.
    setMessage('')
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setMessage(text), 60)
  }, [])
  return (
    <AnnounceContext.Provider value={announce}>
      {children}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {message}
      </div>
    </AnnounceContext.Provider>
  )
}

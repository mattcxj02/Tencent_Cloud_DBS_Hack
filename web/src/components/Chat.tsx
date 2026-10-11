// Chat: capture turns at the gateway, ask the gateway for a draft after each user turn,
// then show a clarification, a draft that needs correcting, or the exact-envelope
// confirmation. Late responses never replace a newer revision.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { uploadVoice } from '../audio/api'
import type { Conversation, DraftBundle, Turn, ValidationView } from '../api'
import { describeCode } from '../messages'
import { diffLegs, fromDraftLeg } from '../legs'
import type { LegChange, LegView } from '../legs'
import { hrefFor } from '../router'
import { useAnnounce, useSession } from '../session'
import { ClarifyCard } from './ClarifyCard'
import { Composer } from './Composer'
import { Confirmation } from './Confirmation'
import { DraftReview } from './DraftReview'
import { FacilitatorPanel, useFacilitatorEnabled } from './FacilitatorPanel'
import { Transcript, visibleTurns } from './Transcript'
import { Button, Notice, PageTitle } from './ui'

const CONV_KEY = 'dcta.conversation_id'

type Flow =
  | { kind: 'none' }
  | { kind: 'clarify'; question: { turn_id: string; text: string } }
  | { kind: 'bundle'; draft: DraftBundle; prevLegs: LegView[] | null; changes: LegChange[] }
  | { kind: 'error'; code: string }

type Busy = null | 'sending' | 'drafting' | 'voice'

function readConv(): string | null {
  try {
    return window.sessionStorage.getItem(CONV_KEY)
  } catch {
    return null
  }
}

function writeConv(id: string | null): void {
  try {
    if (id) window.sessionStorage.setItem(CONV_KEY, id)
    else window.sessionStorage.removeItem(CONV_KEY)
  } catch {
    // storage blocked: the conversation simply is not resumed after a reload
  }
}

function errorCode(e: unknown): string {
  return e instanceof ApiError ? e.code : 'NETWORK_ERROR'
}

/** Approval is offered only for a ready draft whose stored validation is an eligible MATCH. */
function eligible(d: DraftBundle): boolean {
  const v = d.validation
  return d.status === 'ready' && !!v && v.verdict === 'MATCH' && v.eligible_for_confirmation !== false
}

export function ChatPage() {
  const { accounts } = useSession()
  const announce = useAnnounce()
  const facilitator = useFacilitatorEnabled()
  const [conversationId, setConversationId] = useState<string | null>(readConv)
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [flow, setFlow] = useState<Flow>({ kind: 'none' })
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null)
  // After a receipt the conversation is closed: the next message starts a new one, so a
  // paid request can never be drafted (and paid) again from the same words.
  const [completed, setCompleted] = useState(false)
  const completedRef = useRef(false)

  const convRef = useRef<string | null>(conversationId)
  const epoch = useRef(0) // bumped by every user action; older async results are dropped
  const busyRef = useRef(false)
  const latestRevision = useRef(0)
  const lastLegs = useRef<LegView[] | null>(null)

  const turns = useMemo(() => visibleTurns(conversation?.turns ?? []), [conversation])

  const refresh = useCallback(async (id: string) => {
    const c = await api.conversation(id)
    latestRevision.current = Math.max(latestRevision.current, c.revision)
    setConversation(c)
  }, [])

  // Resume the conversation of this browser tab after a reload.
  useEffect(() => {
    const id = convRef.current
    if (!id) return
    refresh(id).catch((e) => {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        convRef.current = null
        setConversationId(null)
        writeConv(null)
      }
    })
  }, [refresh])

  const ensureConversation = async (): Promise<string> => {
    if (completedRef.current) {
      completedRef.current = false
      setCompleted(false)
      convRef.current = null
      lastLegs.current = null
      latestRevision.current = 0
    }
    if (convRef.current) return convRef.current
    const c = await api.createConversation()
    convRef.current = c.conversation_id
    setConversationId(c.conversation_id)
    writeConv(c.conversation_id)
    setConversation(c)
    latestRevision.current = c.revision
    return c.conversation_id
  }

  const noteTurn = (turn: Turn) => {
    if (typeof turn.conversation_revision === 'number') {
      latestRevision.current = Math.max(latestRevision.current, turn.conversation_revision)
    }
  }

  /** Ask the gateway for a draft of the current revision. */
  const draftNow = async (id: string, ticket: number) => {
    setBusy('drafting')
    announce('Preparing your draft and checking it independently…')
    let result
    try {
      result = await api.draft(id)
    } catch (e) {
      if (ticket !== epoch.current) return
      setFlow({ kind: 'error', code: errorCode(e) })
      return
    }
    if (ticket !== epoch.current) return // a newer action happened meanwhile
    if (result.status !== 'error' && result.conversation_revision < latestRevision.current) return // stale
    await refresh(id).catch(() => undefined) // shows the gateway-recorded question turn
    if (ticket !== epoch.current) return
    if (result.status === 'clarify') {
      setFlow({ kind: 'clarify', question: result.question })
      announce(`Question: ${result.question.text}`)
    } else if (result.status === 'error') {
      setFlow({ kind: 'error', code: result.code })
    } else if (result.status === 'ready' || result.status === 'needs_correction') {
      const legs = result.legs.map((l) => fromDraftLeg(l, accounts ?? []))
      const prev = lastLegs.current
      lastLegs.current = legs
      setFlow({ kind: 'bundle', draft: result, prevLegs: prev, changes: prev ? diffLegs(prev, legs) : [] })
    } else {
      setFlow({ kind: 'error', code: 'UNEXPECTED_DRAFT_STATUS' }) // never approve on an unknown shape
    }
  }

  /** Run one user action: no double submits, and only the newest action may update the screen. */
  const run = async (kind: Busy, action: (ticket: number) => Promise<void>) => {
    if (busyRef.current) return
    busyRef.current = true
    const ticket = ++epoch.current
    setBusy(kind)
    setError(null)
    try {
      await action(ticket)
    } catch (e) {
      if (ticket === epoch.current) setError(errorCode(e))
    } finally {
      if (ticket === epoch.current) {
        busyRef.current = false
        setBusy(null)
      }
    }
  }

  const sendText = (text: string) =>
    run('sending', async (ticket) => {
      const id = await ensureConversation()
      const question = flow.kind === 'clarify' ? flow.question : null
      const turn = question ? await api.answer(id, question.turn_id, text) : await api.appendText(id, text)
      noteTurn(turn)
      setFlow({ kind: 'none' })
      setEditingTurnId(null)
      await refresh(id)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const saveEdit = (turnId: string, text: string) =>
    run('sending', async (ticket) => {
      const id = await ensureConversation()
      noteTurn(await api.editTranscript(id, turnId, text))
      setEditingTurnId(null)
      setFlow({ kind: 'none' }) // the old draft and any pending approval are superseded
      announce('Correction saved. Preparing a new draft.')
      await refresh(id)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const sendVoice = (audio: Blob) =>
    run('voice', async (ticket) => {
      const id = await ensureConversation()
      announce('Transcribing your voice…')
      const res = await uploadVoice(id, audio) // sends consent=true; gateway stores the exact WAV
      if (!res.turn) {
        await refresh(id)
        setError('ASR_NO_SPEECH')
        return
      }
      noteTurn(res.turn)
      setFlow({ kind: 'none' })
      await refresh(id)
      announce(`Heard: ${res.turn.text}. You can correct it if it is wrong.`)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const redraft = () =>
    run('drafting', async (ticket) => {
      const id = convRef.current
      if (!id) return
      setFlow({ kind: 'none' })
      await draftNow(id, ticket)
    })

  const editLatest = () => {
    const lastUser = [...turns].reverse().find((t) => t.current.role !== 'app_question')
    if (lastUser) setEditingTurnId(lastUser.current.turn_id)
  }

  const dismiss = () => {
    epoch.current++ // drop any late draft result
    busyRef.current = false
    setBusy(null)
    setFlow({ kind: 'none' })
  }

  const markCommitted = useCallback(() => {
    completedRef.current = true
    setCompleted(true)
    setEditingTurnId(null)
  }, [])

  const newConversation = () => {
    epoch.current++
    completedRef.current = false
    setCompleted(false)
    busyRef.current = false
    setBusy(null)
    convRef.current = null
    setConversationId(null)
    writeConv(null)
    setConversation(null)
    setFlow({ kind: 'none' })
    setError(null)
    setEditingTurnId(null)
    lastLegs.current = null
    latestRevision.current = 0
    announce('New conversation started.')
  }

  const onValidated = useCallback((v: ValidationView) => {
    setFlow((f) => (f.kind === 'bundle' ? { ...f, draft: { ...f.draft, validation: v } } : f))
  }, [])

  const pendingQuestion = flow.kind === 'clarify'

  return (
    <div className="space-y-4">
      <PageTitle sub="Type or speak. You will see the exact details, checked independently, before anything is paid.">
        Payments assistant
      </PageTitle>

      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-700">
        <span>
          {conversationId ? (
            <>
              Conversation revision {conversation?.revision ?? '…'} ·{' '}
              <a className="font-semibold text-indigo-800 underline" href={hrefFor('evidence', conversationId)}>
                Evidence trail
              </a>
            </>
          ) : (
            'No conversation yet'
          )}
        </span>
        <Button variant="secondary" onClick={newConversation} disabled={busy === 'sending'}>
          New conversation
        </Button>
      </div>

      <Transcript
        turns={turns}
        editingTurnId={editingTurnId}
        busy={busy !== null || completed}
        onStartEdit={setEditingTurnId}
        onCancelEdit={() => setEditingTurnId(null)}
        onSaveEdit={saveEdit}
      />

      {busy === 'drafting' && (
        <Notice kind="info" title="Preparing your draft and checking it independently…">
          <p className="text-sm">This can take a few seconds.</p>
        </Notice>
      )}
      {busy === 'voice' && <Notice kind="info" title="Transcribing your voice…" />}

      {error && (
        <Notice kind="error" title={describeCode(error)} focus>
          {error === 'CONTEXT_LIMIT_RESTATE' && <Button onClick={newConversation}>Start a new conversation</Button>}
        </Notice>
      )}

      {flow.kind === 'clarify' && (
        <ClarifyCard question={flow.question} disabled={busy !== null} onChoose={sendText} />
      )}

      {flow.kind === 'bundle' &&
        (eligible(flow.draft) ? (
          <Confirmation
            key={flow.draft.bundle_id}
            draft={flow.draft}
            prevLegs={flow.prevLegs}
            onEdit={editLatest}
            onRedraft={redraft}
            onClose={dismiss}
            onCommitted={markCommitted}
            onFinished={newConversation}
          />
        ) : (
          <DraftReview
            key={flow.draft.bundle_id}
            draft={flow.draft}
            changes={flow.changes}
            turns={conversation?.turns ?? []}
            onEdit={editLatest}
            onRedraft={redraft}
            onDismiss={dismiss}
            onNewConversation={newConversation}
            onValidated={onValidated}
          />
        ))}

      {flow.kind === 'error' && (
        <Notice kind="error" title="We couldn't prepare a draft" focus>
          <p>{describeCode(flow.code)}</p>
          <p className="text-sm">Nothing was paid. Your messages are saved.</p>
          <div className="flex flex-wrap gap-2">
            <Button onClick={redraft}>Try again</Button>
            {(flow.code === 'CONTEXT_LIMIT_RESTATE' || flow.code === 'CONVERSATION_NOT_FOUND') && (
              <Button variant="secondary" onClick={newConversation}>
                Start a new conversation
              </Button>
            )}
          </div>
        </Notice>
      )}

      {completed && flow.kind === 'none' && (
        <Notice kind="info" title="This conversation is complete">
          <p>Your next message starts a new conversation.</p>
        </Notice>
      )}

      {flow.kind === 'none' && !completed && busy === null && turns.some((t) => t.current.role !== 'app_question') && (
        <div>
          <Button variant="secondary" onClick={redraft}>
            Prepare a draft from this conversation
          </Button>
        </div>
      )}

      <Composer
        label={pendingQuestion ? 'Your answer' : 'Your request'}
        placeholder={pendingQuestion ? 'Type your answer…' : 'e.g. Send Mum 50 dollars from savings'}
        disabled={busy !== null}
        onSend={sendText}
        onRecorded={sendVoice}
      />

      {facilitator && <FacilitatorPanel scope="DCTA chat" />}
    </div>
  )
}

// Chat: capture turns at the gateway, ask the gateway for a draft after each user turn,
// then show a clarification, a draft that needs correcting, or the exact-envelope
// confirmation. Late responses never replace a newer revision.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { readProgress, savedOperation, saveOperation, stopDraft, watchProgress, type Operation, type Progress } from '../progress'
import { ProgressNotice } from './ProgressNotice'
import { ExistingPayment } from './ExistingPayment'
import { uploadVoice, type VoiceResponse } from '../audio/api'
import type { ClarificationQuestion, Conversation, DraftBundle, DraftResult, StructuredAnswer, Turn, ValidationView } from '../api'
import { describeCode, isConfigurationError } from '../messages'
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
  | { kind: 'clarify'; question: ClarificationQuestion }
  | { kind: 'bundle'; draft: DraftBundle; prevLegs: LegView[] | null; changes: LegChange[] }
  | { kind: 'error'; code: string; validationOnly?: boolean }
  | { kind: 'recover'; authorizationId: string }
  | { kind: 'recovery-error'; code: string }

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
  const [resumeAttempt, setResumeAttempt] = useState(0)
  const [progress, setProgress] = useState<Progress | null>(null)
  const operation = useRef<Operation | null>(null)
  const controller = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null)
  const [composerText, setComposerText] = useState('')
  // After a receipt the conversation is closed: the next message starts a new one, so a
  // paid request can never be drafted (and paid) again from the same words.
  const [approvalPending, setApprovalPending] = useState(false)
  const [completed, setCompleted] = useState(false)
  const completedRef = useRef(false)

  const convRef = useRef<string | null>(conversationId)
  const epoch = useRef(0) // bumped by every user action; older async results are dropped
  const busyRef = useRef(false)
  const latestRevision = useRef(0)
  const lastLegs = useRef<LegView[] | null>(null)

  const turns = useMemo(() => visibleTurns(conversation?.turns ?? []), [conversation])

  const refresh = useCallback(async (id: string, ticket = epoch.current) => {
    const c = await api.conversation(id)
    if (ticket !== epoch.current || convRef.current !== id) return
    latestRevision.current = c.revision
    setConversation(c)
  }, [])

  // Recover payment state before restoring drafting work after a reload.
  useEffect(() => {
    const ticket = epoch.current
    const id = convRef.current
    const recovery = new AbortController()
    controller.current = recovery
    let evidenceRead = false
    const resume = async () => {
      if (!id) return
      busyRef.current = true
      setBusy('sending')
      await refresh(id, ticket)
      const evidence = await api.evidence(id)
      evidenceRead = true
      if (ticket !== epoch.current) return
      const existing = [...evidence.authorizations].reverse().find((a) =>
        ['COMMITTED', 'PENDING_APPROVAL', 'AWAITING_STEP_UP'].includes(a.status))
      if (existing) {
        setFlow({ kind: 'recover', authorizationId: existing.authorization_id })
        return
      }
      const op = savedOperation()
      if (op?.conversationId === id) {
        operation.current = op
        setBusy(op.kind === 'voice' ? 'voice' : 'drafting')
        await readProgress(op, recovery.signal)
        const p = await watchProgress(op, recovery.signal, (p) => {
          if (ticket === epoch.current) setProgress(p)
        })
        if (ticket !== epoch.current) return
        if (p?.code) setFlow({ kind: 'error', code: p.code, validationOnly: p.retry_validation })
        else if (p?.result && op.kind === 'draft') await applyDraft(id, ticket, p.result as DraftResult)
        else if (p?.result && op.kind === 'voice') {
          await refresh(id, ticket)
          await draftNow(id, ticket)
        } else setFlow({ kind: 'error', code: 'PROGRESS_NOT_FOUND' })
      }
    }
    void resume().catch(async (e) => {
      if (ticket !== epoch.current) return
      if (e instanceof ApiError && ['CONVERSATION_CHANGED', 'STALE_CONVERSATION_REVISION'].includes(e.code)) {
        await refresh(id!, ticket)
        await draftNow(id!, ticket, false, false)
      } else if (e instanceof ApiError && e.status === 404 && e.code !== 'PROGRESS_NOT_FOUND') {
        convRef.current = null; setConversationId(null); writeConv(null)
      } else setFlow({ kind: evidenceRead ? 'error' : 'recovery-error', code: errorCode(e) })
    }).finally(() => {
      if (ticket === epoch.current) { busyRef.current = false; setBusy(null) }
    })
    return () => { epoch.current++; recovery.abort(); controller.current?.abort() }
  }, [refresh, resumeAttempt])

  const ensureConversation = async (ticket: number): Promise<string> => {
    if (completedRef.current) {
      completedRef.current = false
      setCompleted(false)
      convRef.current = null
      lastLegs.current = null
      latestRevision.current = 0
    }
    if (convRef.current) return convRef.current
    const c = await api.createConversation()
    if (ticket !== epoch.current) throw new ApiError(0, 'REQUEST_CANCELLED', [], null)
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

  const applyDraft = async (id: string, ticket: number, result: DraftResult) => {
    if (ticket !== epoch.current || convRef.current !== id) return false
    await refresh(id, ticket).catch(() => undefined)
    if (ticket !== epoch.current || convRef.current !== id) return false
    if (result.status !== 'error' && result.conversation_revision !== latestRevision.current) return false
    if (result.status === 'clarify') {
      setFlow({ kind: 'clarify', question: result.question })
      announce(`Question: ${result.question.text}`)
    } else if (result.status === 'error') {
      setFlow({ kind: 'error', code: result.code })
    } else {
      const legs = result.legs.map((l) => fromDraftLeg(l, accounts ?? []))
      const prev = lastLegs.current
      lastLegs.current = legs
      setFlow({ kind: 'bundle', draft: result, prevLegs: prev, changes: prev ? diffLegs(prev, legs) : [] })
    }
    return true
  }

  /** Stale input gets one automatic rebuild; validation retries must reuse frozen work. */
  const draftNow = async (id: string, ticket: number, validationOnly = false, rebuild = true): Promise<void> => {
    if (ticket !== epoch.current) return
    setBusy('drafting')
    const op: Operation = { conversationId: id, revision: latestRevision.current,
      operationId: crypto.randomUUID(), kind: 'draft' }
    const control = new AbortController()
    controller.current = control
    operation.current = op
    saveOperation(op)
    setProgress(null)
    let last: Progress | null = null
    const polling = watchProgress(op, control.signal, (p) => {
      last = p
      if (ticket === epoch.current && convRef.current === id) setProgress(p)
    }).catch(() => null)
    try {
      const result = await api.draft(id, { operation_id: op.operationId, revision: op.revision,
        mode: validationOnly ? 'validation' : 'auto', signal: control.signal })
      if (result.status === 'error') throw new ApiError(503, result.code, [], null)
      if (!await applyDraft(id, ticket, result) && ticket === epoch.current) {
        throw new ApiError(409, 'CONVERSATION_CHANGED', [], {})
      }
    } catch (e) {
      if (ticket !== epoch.current || control.signal.aborted) return
      const code = errorCode(e)
      if (rebuild && ['CONVERSATION_CHANGED', 'STALE_CONVERSATION_REVISION'].includes(code)) {
        control.abort()
        await refresh(id, ticket)
        await draftNow(id, ticket, false, false)
        return
      }
      if (e instanceof ApiError && e.network) {
        const recovered = await polling
        if (ticket !== epoch.current) return
        if (recovered?.result) { await applyDraft(id, ticket, recovered.result as DraftResult); return }
      }
      last = await readProgress(op, control.signal).catch(() => last)
      if (ticket === epoch.current) setFlow({ kind: 'error', code: last?.code ?? code,
        validationOnly: validationOnly || last?.retry_validation })
    } finally {
      control.abort()
      if (operation.current === op) operation.current = null
    }
  }

  const stopProcessing = () => {
    const op = operation.current
    controller.current?.abort()
    if (op?.kind === 'draft') void stopDraft(op).catch(() => undefined)
    operation.current = null
    saveOperation(null)
  }

  /** Run one user action: no double submits, and only the newest action may update the screen. */
  const run = async (kind: Busy, action: (ticket: number) => Promise<void>) => {
    if (busyRef.current) {
      if (kind !== 'sending' || operation.current?.kind !== 'draft') return
      stopProcessing()
    }
    busyRef.current = true
    const ticket = ++epoch.current
    setBusy(kind)
    if (kind === 'sending' || kind === 'voice') setFlow({ kind: 'none' })
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

  const sendText = (text: string | StructuredAnswer) =>
    run('sending', async (ticket) => {
      const id = await ensureConversation(ticket)
      const question = flow.kind === 'clarify' ? flow.question : null
      if (!question && typeof text !== 'string') return
      const turn = question ? await api.answer(id, question.turn_id, text) : await api.appendText(id, text as string)
      if (ticket !== epoch.current || convRef.current !== id) return
      noteTurn(turn)
      setFlow({ kind: 'none' })
      setEditingTurnId(null)
      await refresh(id, ticket)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const saveEdit = (turnId: string, text: string) =>
    run('sending', async (ticket) => {
      const id = await ensureConversation(ticket)
      const turn = await api.editTranscript(id, turnId, text)
      if (ticket !== epoch.current || convRef.current !== id) return
      noteTurn(turn)
      setEditingTurnId(null)
      setFlow({ kind: 'none' }) // the old draft and any pending approval are superseded
      announce('Correction saved. Preparing a new draft.')
      await refresh(id, ticket)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const sendVoice = (audio: Blob) =>
    run('voice', async (ticket) => {
      const id = await ensureConversation(ticket)
      const op: Operation = { conversationId: id, revision: latestRevision.current,
        operationId: crypto.randomUUID(), kind: 'voice' }
      const control = new AbortController()
      controller.current = control; operation.current = op; saveOperation(op); setProgress(null)
      const polling = watchProgress(op, control.signal, (p) => {
        if (ticket === epoch.current) setProgress(p)
      }).catch(() => null)
      let res
      try { res = await uploadVoice(id, audio, op.operationId, control.signal) }
      catch (e) {
        if (!(e instanceof ApiError) || !e.network) throw e
        const recovered = await polling
        if (!recovered?.result || recovered.kind !== 'voice') throw e
        res = recovered.result as VoiceResponse
      } finally { control.abort(); if (operation.current === op) operation.current = null }
      if (ticket !== epoch.current) return
      if (!res.turn) {
        await refresh(id)
        setError('ASR_NO_SPEECH')
        return
      }
      noteTurn(res.turn)
      setFlow({ kind: 'none' })
      await refresh(id, ticket)
      if (ticket !== epoch.current) return
      announce(`Heard: ${res.turn.text}. You can correct it if it is wrong.`)
      if (ticket === epoch.current) await draftNow(id, ticket)
    })

  const redraft = (validationOnly = false) =>
    run('drafting', async (ticket) => {
      const id = convRef.current
      if (!id) return
      setFlow({ kind: 'none' })
      await draftNow(id, ticket, validationOnly)
    })

  const editLatest = () => {
    const lastUser = [...turns].reverse().find((t) => t.current.role !== 'app_question')
    if (lastUser) setEditingTurnId(lastUser.current.turn_id)
  }

  const dismiss = () => {
    if (operation.current) announce('Stopped waiting for this draft. Approved payments are unchanged.')
    stopProcessing()
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
    stopProcessing()
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
            <a className="font-semibold text-indigo-800 underline" href={hrefFor('evidence', conversationId)}>
              Evidence trail for this conversation
            </a>
          ) : (
            'No conversation yet'
          )}
        </span>
        <Button variant="ghost" onClick={newConversation} disabled={approvalPending || busy === 'sending' || (flow.kind === 'recover' || flow.kind === 'recovery-error')}>
          New conversation
        </Button>
      </div>

      <Transcript
        turns={turns}
        editingTurnId={editingTurnId}
        busy={approvalPending || (busy !== null && busy !== 'drafting') || completed || (flow.kind === 'recover' || flow.kind === 'recovery-error')}
        onStartEdit={setEditingTurnId}
        onCancelEdit={() => setEditingTurnId(null)}
        onSaveEdit={saveEdit}
        onExample={facilitator ? undefined : (text) => {
          setComposerText(text)
          document.getElementById('composer-input')?.focus()
        }}
      />

      {(busy === 'drafting' || busy === 'voice') && <>
        <ProgressNotice progress={progress} />
        {busy === 'drafting' && <Button variant="secondary" onClick={dismiss}>Stop preparing draft</Button>}
        {busy === 'drafting' && <p className="text-sm">You can still correct what you said above; the draft is then prepared again.</p>}
      </>}

      {flow.kind === 'recovery-error' && <Notice kind="error" title="Payment state is not confirmed yet">
        <p>Your saved conversation may have an approval or receipt. Check it before starting another payment.</p>
        <Button onClick={() => setResumeAttempt((a) => a + 1)}>Check existing payment</Button>
      </Notice>}

      {flow.kind === 'recover' && <ExistingPayment authorizationId={flow.authorizationId}
        onCommitted={markCommitted} onFinished={newConversation}
        onEdit={() => { setFlow({ kind: 'none' }); editLatest() }}
        onRedraft={() => redraft()} onClose={dismiss} />}

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
            onRedraft={() => redraft()}
            onClose={dismiss}
            onCommitted={markCommitted}
            onPendingChange={setApprovalPending}
            onFinished={newConversation}
          />
        ) : (
          <DraftReview
            key={flow.draft.bundle_id}
            draft={flow.draft}
            changes={flow.changes}
            turns={conversation?.turns ?? []}
            onEdit={editLatest}
            onRedraft={() => redraft()}
            onDismiss={dismiss}
            onNewConversation={newConversation}
            onValidated={onValidated}
            onRetryValidation={() => redraft(true)}
          />
        ))}

      {flow.kind === 'error' && (
        <Notice kind="error" title="We couldn't prepare a draft" focus>
          <p>{describeCode(flow.code)}</p>
          <p className="text-sm">Draft preparation cannot move money. Your messages are saved.</p>
          <div className="flex flex-wrap gap-2">
            {!isConfigurationError(flow.code) && <Button onClick={() => redraft(flow.validationOnly)}>{flow.validationOnly ? 'Retry validation of saved draft' : 'Prepare draft again'}</Button>}
            {(flow.code === 'CONTEXT_LIMIT_RESTATE' || flow.code === 'CONVERSATION_NOT_FOUND') && (
              <Button variant="ghost" onClick={newConversation}>
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
          <Button variant="secondary" onClick={() => redraft()}>
            Prepare a draft from this conversation
          </Button>
        </div>
      )}

      <Composer
        label={pendingQuestion ? 'Your answer' : 'Your request'}
        placeholder={pendingQuestion ? 'Type your answer…' : 'e.g. Send Mum 50 dollars from savings'}
        disabled={approvalPending || busy !== null || (flow.kind === 'recover' || flow.kind === 'recovery-error')}
        prominent={flow.kind === 'none' || flow.kind === 'clarify'}
        text={composerText}
        onTextChange={setComposerText}
        onSend={sendText}
        onRecorded={sendVoice}
      />

      {facilitator && <FacilitatorPanel scope="DCTA chat" />}
    </div>
  )
}

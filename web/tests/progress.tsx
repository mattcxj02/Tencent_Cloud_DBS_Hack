// Real components and transport, synthetic gateway responses; no payment or model calls.
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, request, type Conversation, type DraftResult, type AuthorizationView, type EvidenceView } from '../src/api'
import { watchProgress, saveOperation, type Progress, type Operation } from '../src/progress'
import { AnnouncerProvider, SessionProvider } from '../src/session'
import { ChatPage } from '../src/components/Chat'
import { ProgressNotice } from '../src/components/ProgressNotice'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const host = document.getElementById('fixture')!
const results = document.getElementById('results')!
let root = createRoot(host)
const passed: string[] = []
const op: Operation = { conversationId: 'synthetic', operationId: crypto.randomUUID(), revision: 1, kind: 'draft' }
const p: Progress = { operation_id: op.operationId, conversation_revision: 1, kind: 'draft', stage: 'extracting',
  elapsed_seconds: 7, done: false, code: null, result: null, retry_validation: false }
const text = 'Send Mum fifty dollars from savings'
let conv: Conversation
let evidence: EvidenceView
let draftRequests: Parameters<typeof api.draft>[1][] = []
let authorizationCreates = 0
const originalApi = { ...api }
const originalFetch = window.fetch
const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
function check(condition: unknown, message: string): asserts condition { if (!condition) throw new Error(message) }
function button(label: string) {
  const b = [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)
  check(b, `Missing button: ${label}`)
  return b
}
async function reset() {
  await act(async () => root.unmount())
  root = createRoot(host)
  Object.assign(api, originalApi)
  window.fetch = originalFetch
  sessionStorage.removeItem('dcta.operation')
  sessionStorage.setItem('dcta.conversation_id', 'synthetic')
  conv = { conversation_id: 'synthetic', revision: 1, turns: [{ turn_id: 'voice-1', seq: 1, role: 'user_asr_final',
    text, edits_turn_id: null, answers_question_id: null, created_at: '2026-10-13T00:00:00Z' }] } as Conversation
  evidence = { authorizations: [], drafts: [], turns: conv.turns } as unknown as EvidenceView
  draftRequests = []; authorizationCreates = 0
  api.me = async () => ({ user_id: 'synthetic-owner', display_name: 'Fixture', credentials: [{ credential_id: 'credential', created_at: '2026-10-13' }] })
  api.accounts = async () => []
  api.conversation = async () => structuredClone(conv)
  api.evidence = async () => evidence
  api.createAuthorization = async () => { authorizationCreates++; throw new Error('Unexpected new authorization') }
  // All progress reads are stubbed; no real gateway fetch can escape the fixture.
  window.fetch = async () => new Response(JSON.stringify(p), { status: 200 })
}
async function renderChat() {
  await act(async () => { root.render(<SessionProvider><AnnouncerProvider><ChatPage /></AnnouncerProvider></SessionProvider>); await delay(10) })
}
const clarify = (revision: number): DraftResult => ({ status: 'clarify', conversation_revision: revision,
  question: { turn_id: `question-${revision}`, text: 'Which account?' } })
async function click(label: string) { await act(async () => { button(label).click(); await delay(10) }) }
function setText(value: string) {
  const input = host.querySelector<HTMLTextAreaElement>('#turn-editor')!
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function run() {
  await reset()
  window.fetch = async (_input, init) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('Stopped', 'AbortError')))
  })
  try { await request('GET', '/fixture', { timeoutMs: 5 }); throw new Error('Request should time out') }
  catch (e) { check(e instanceof ApiError && e.code === 'REQUEST_TIMEOUT' && e.network, 'Bounded unknown request') }
  passed.push('Browser requests time out with an unknown-result error')

  window.fetch = async (_input, init) => new Response(new ReadableStream({
    start(stream) { init?.signal?.addEventListener('abort', () => stream.error(new DOMException('Stopped', 'AbortError'))) },
  }))
  try { await request('GET', '/slow-body', { timeoutMs: 5 }); throw new Error('Body should time out') }
  catch (e) { check(e instanceof ApiError && e.code === 'REQUEST_TIMEOUT', 'Deadline covers response body') }
  passed.push('Browser deadlines also bound a stalled response body')

  await reset()
  let reads = 0
  window.fetch = async (input) => {
    check(String(input).includes(`operation_id=${op.operationId}`) && String(input).includes('revision=1'), 'Poll binds operation and revision')
    reads++
    return new Response(JSON.stringify({ ...p, stage: 'ready', done: true }))
  }
  const control = new AbortController()
  const seen: Progress[] = []
  const terminal = await watchProgress(op, control.signal, (v) => seen.push(v))
  check(terminal?.done && reads === 1 && seen.length === 1, 'Completion stops polling')
  passed.push('Owned operation/revision query stops immediately on completion')

  let resolveRead!: (response: Response) => void
  window.fetch = async () => new Promise<Response>((resolve) => { resolveRead = resolve })
  const cancelled = new AbortController()
  const watching = watchProgress(op, cancelled.signal, (v) => seen.push(v))
  cancelled.abort(); resolveRead(new Response(JSON.stringify(p)))
  await watching
  check(seen.length === 1, 'A late cancelled read must not publish a stage')
  passed.push('Late polling response is discarded after cancellation/unmount')

  await reset()
  const originalInterval = window.setInterval
  let tick: (() => void) | undefined
  window.setInterval = ((handler: TimerHandler) => { tick = handler as () => void; return 999 }) as typeof window.setInterval
  await act(async () => { root.render(<AnnouncerProvider><ProgressNotice progress={p} /></AnnouncerProvider>) })
  await act(async () => { await delay(80) })
  const live = host.querySelector('[role="status"]')!
  const observer = new MutationObserver(() => { announcements++ })
  let announcements = 0
  observer.observe(live, { childList: true, characterData: true, subtree: true })
  await act(async () => { tick?.(); await delay(80) })
  check(live.textContent === 'Reading your request' && announcements === 0, 'Seconds must not announce stages')
  await act(async () => { root.render(<AnnouncerProvider><ProgressNotice progress={{ ...p, stage: 'validating' }} /></AnnouncerProvider>) })
  await act(async () => { await delay(80) })
  check(String(live.textContent) === 'Checking the frozen draft independently', 'Actual stage change announces')
  observer.disconnect(); window.setInterval = originalInterval
  passed.push('Stage changes announce; elapsed ticks stay silent')

  await reset()
  api.draft = async (_id, opts) => {
    draftRequests.push(opts)
    if (draftRequests.length === 1) { conv.revision = 2; throw new ApiError(409, 'STALE_CONVERSATION_REVISION', [], {}) }
    return clarify(2)
  }
  await renderChat()
  await click('Prepare a draft from this conversation')
  check(draftRequests.length === 2 && draftRequests[0]?.revision === 1 && draftRequests[1]?.revision === 2, 'Automatically rebuild at latest revision once')
  check(host.textContent?.includes('Which account?'), 'Publish latest result')
  passed.push('Changed conversation automatically drafts once at the latest revision')

  await reset()
  api.draft = async (_id, opts) => { draftRequests.push(opts); conv.revision++; throw new ApiError(409, 'CONVERSATION_CHANGED', [], {}) }
  await renderChat(); await click('Prepare a draft from this conversation')
  check(draftRequests.length === 2, 'Continuous changes must not create a retry loop')
  passed.push('Automatic rebuild is bounded to one retry')

  await reset()
  let releaseDraft!: (r: DraftResult) => void
  api.draft = async (_id, opts) => {
    draftRequests.push(opts)
    if (draftRequests.length === 1) return new Promise((resolve) => { releaseDraft = resolve })
    return clarify(2)
  }
  api.editTranscript = async (_id, turnId, corrected) => {
    conv.revision = 2
    const turn = { ...conv.turns[0], turn_id: 'edit-2', role: 'user_transcript_edit' as const,
      text: corrected, edits_turn_id: turnId, conversation_revision: 2 }
    conv.turns.push(turn)
    return turn
  }
  await renderChat(); await click('Prepare a draft from this conversation'); await click('Correct this transcript')
  await act(async () => setText('Send Mum fifteen dollars from savings'))
  await click('Save correction')
  await act(async () => { releaseDraft({ status: 'ready', conversation_revision: 1, bundle_id: 'old', bundle_hash: 'old',
    legs: [], total_debit: '50.00', validation: { verdict: 'MATCH', eligible_for_confirmation: true } as never }); await delay(10) })
  check(draftRequests.length === 2 && draftRequests[1]?.revision === 2 && authorizationCreates === 0, 'Correction supersedes old ready result')
  check(host.textContent?.includes('fifteen') && host.textContent?.includes('Which account?'), 'Corrected transcript remains understandable')
  passed.push('Voice transcript correction while drafting drops late confirmation without another confirmation click')

  await reset()
  api.draft = async () => { throw new ApiError(503, 'SERVICE_TOKEN_UNSET', [], {}) }
  await renderChat(); await click('Prepare a draft from this conversation')
  check(![...host.querySelectorAll('button')].some((b) => b.textContent?.includes('draft again')), 'Permanent setup failures have no retry loop')
  passed.push('Permanent configuration failure explains the problem without a retry button')

  await reset()
  window.fetch = async () => new Response(JSON.stringify({ ...p, done: true, stage: 'failed', code: 'VALIDATOR_UNAVAILABLE', retry_validation: true }))
  api.draft = async (_id, opts) => {
    draftRequests.push(opts)
    if (draftRequests.length === 1) throw new ApiError(503, 'VALIDATOR_UNAVAILABLE', [], {})
    return clarify(1)
  }
  await renderChat(); await click('Prepare a draft from this conversation')
  await click('Retry validation of saved draft')
  check(draftRequests[1]?.mode === 'validation', 'Validation retry must send explicit frozen-work intent')
  passed.push('Retry validation is distinct from rebuilding a corrected draft')

  await reset()
  let releaseOld!: (r: DraftResult) => void
  api.draft = async () => new Promise((resolve) => { releaseOld = resolve })
  await renderChat(); await click('Prepare a draft from this conversation'); await click('New conversation')
  await act(async () => { releaseOld(clarify(1)); await delay(10) })
  check(!host.textContent?.includes('Which account?') && host.textContent?.includes('No conversation yet'), 'Navigation discards old draft response')
  passed.push('A late response cannot replace a newly started conversation')

  await reset()
  saveOperation(op)
  window.fetch = async () => new Response(JSON.stringify({ ...p, done: true, stage: 'needs_input', result: clarify(1) }))
  api.draft = async () => { throw new Error('Reload must not redraft completed work') }
  await renderChat()
  check(host.textContent?.includes('Which account?'), 'Reload restores stored operation result')
  passed.push('Reload restores the existing progress result without new model work')

  await reset()
  api.evidence = async () => { throw new ApiError(0, 'REQUEST_TIMEOUT', [], null) }
  await renderChat()
  check(host.textContent?.includes('Payment state is not confirmed yet'), 'Unknown payment state must be explicit')
  check(button('New conversation').disabled && !host.textContent?.includes('Nothing was paid'), 'Unknown result must not suggest a new payment')
  passed.push('Failed reload recovery blocks a new payment until existing state can be read')

  await reset()
  evidence.authorizations = [{ authorization_id: 'existing', status: 'COMMITTED' }] as EvidenceView['authorizations']
  let receipts = 0
  api.authorization = async () => ({ authorization_id: 'existing', status: 'COMMITTED',
    envelope: { issued_at: '2026-10-13T00:00:00Z', payload: { bundle_id: 'paid', conversation_id: 'synthetic',
      conversation_revision: 1, legs: [] } } }) as unknown as AuthorizationView
  api.validation = async () => { throw new Error('No current validation needed for a receipt') }
  api.receipt = async () => { receipts++; return { authorization_id: 'existing', status: 'COMMITTED', receipt_id: 'receipt',
    receipt: { receipt_id: 'receipt', authorization_id: 'existing', operation: 'execute_bundle', operation_id: 'paid',
      envelope_hash: 'hash', payload_hash: 'hash', committed_at: '2026-10-13T00:00:00Z', total_debit: '50.00', legs: [], simulated: true } } }
  await renderChat(); await act(async () => { await delay(20) })
  check(receipts === 1 && authorizationCreates === 0 && host.textContent?.includes('Recovered receipt'), 'Recover same authorization receipt before payment suggestions')
  passed.push('Reload after lost approval reads the existing receipt and never creates another authorization')

  results.textContent = `${passed.length} PASS\n${passed.join('\n')}`
}
run().catch((error) => { results.textContent = `FAIL after ${passed.length} checks: ${String(error)}` }).finally(async () => {
  await act(async () => root.unmount())
  Object.assign(api, originalApi); window.fetch = originalFetch
})

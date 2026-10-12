import { ApiError, request, type DraftResult } from './api'
import type { VoiceResponse } from './audio/api'

export interface Operation {
  conversationId: string
  operationId: string
  revision: number
  kind: 'draft' | 'voice'
}

export interface Progress {
  operation_id: string
  conversation_revision: number
  kind: 'draft' | 'voice'
  stage: 'captured' | 'transcribing' | 'extracting' | 'resolving' | 'validating' | 'ready' | 'needs_input' | 'failed'
  elapsed_seconds: number
  done: boolean
  code: string | null
  retry_validation: boolean
  result: DraftResult | VoiceResponse | null
}

export const STAGES: Record<Progress['stage'], string> = {
  captured: 'Input captured', transcribing: 'Transcribing your recording',
  extracting: 'Reading your request', resolving: 'Resolving payment details',
  validating: 'Checking the frozen draft independently', ready: 'Draft ready',
  needs_input: 'Your input is needed', failed: 'Processing stopped',
}

const KEY = 'dcta.operation'
export function savedOperation(): Operation | null {
  try { return JSON.parse(sessionStorage.getItem(KEY) ?? 'null') as Operation | null } catch { return null }
}
export function saveOperation(op: Operation | null) {
  try {
    if (op) sessionStorage.setItem(KEY, JSON.stringify(op))
    else sessionStorage.removeItem(KEY)
  } catch { /* Recovery can also use the owned evidence view. */ }
}

export function readProgress(op: Operation, signal?: AbortSignal) {
  return request<Progress>('GET', `/conversations/${encodeURIComponent(op.conversationId)}/progress?operation_id=${encodeURIComponent(op.operationId)}&revision=${op.revision}`, { signal, timeoutMs: 5000 })
}

export function stopDraft(op: Operation) {
  return request('POST', `/conversations/${encodeURIComponent(op.conversationId)}/draft/cancel`, {
    json: { operation_id: op.operationId, revision: op.revision }, timeoutMs: 5000,
  })
}

function pause(signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
    const timer = window.setTimeout(done, 1000)
    signal.addEventListener('abort', done, { once: true })
    if (signal.aborted) done()
  })
}

/** One outstanding read at a time; finite even if a remote provider ignores its deadline. */
export async function watchProgress(op: Operation, signal: AbortSignal, update: (p: Progress) => void): Promise<Progress | null> {
  const until = Date.now() + 120000
  for (let reads = 0; reads < 100 && Date.now() < until && !signal.aborted; reads++) {
    try {
      const p = await readProgress(op, signal)
      if (signal.aborted) return null
      if (p.conversation_revision !== op.revision || p.kind !== op.kind) return null
      update(p)
      if (p.done) return p
    } catch (e) {
      if (e instanceof ApiError && !e.network && e.code !== 'PROGRESS_NOT_FOUND') throw e
    }
    await pause(signal)
  }
  return null
}

// Typed client for the gateway's user routes. The browser talks only to the gateway
// (`/api/*`, proxied by Vite). The session is an HttpOnly cookie: no token is ever held
// here. Errors surface as ApiError with the gateway's stable code; callers key on codes.
import type { startAuthentication, startRegistration } from '@simplewebauthn/browser'

export type RequestOptionsJSON = Parameters<typeof startAuthentication>[0]['optionsJSON']
export type RegistrationOptionsJSON = Parameters<typeof startRegistration>[0]['optionsJSON']
export type AssertionJSON = Awaited<ReturnType<typeof startAuthentication>>

// ── Identity, accounts, health ───────────────────────────────────────

export interface Health {
  service: string
  status: string
  trtc?: string
  langfuse?: string
}

export interface Me {
  user_id: string
  display_name: string
  credentials: { credential_id: string; created_at: string }[]
}

export interface Account {
  account_id: string
  display: string
  balance: string
}

// ── Conversation ─────────────────────────────────────────────────────

export type TurnRole =
  | 'user_text'
  | 'user_asr_final'
  | 'user_transcript_edit'
  | 'user_answer'
  | 'app_question'
  | string

export interface Turn {
  turn_id: string
  seq: number
  role: TurnRole
  text: string
  edits_turn_id: string | null
  answers_question_id: string | null
  created_at: string
  conversation_revision?: number | null
  conversation_hash?: string | null
}

export interface Conversation {
  conversation_id: string
  owner_user_id: string
  revision: number
  conversation_hash: string
  created_at: string
  updated_at: string
  turns: Turn[]
}

// ── Drafts and validation ────────────────────────────────────────────

export interface EquityTerms {
  budget: string
  quote_id?: string
  unit_price: string
  quoted_at?: string
  quote_expires_at?: string
  quantity: number
  actual_debit: string
  residual_cash: string
}

export interface DraftLeg {
  leg_id: string
  action: string
  amount: string
  currency: string
  display_name: string
  masked_account_suffix: string | null
  source_account_id: string
  source_display?: string | null
  reference: string | null
  equity_terms: EquityTerms | null
}

export interface EvidenceSpan {
  turn_id: string
  start: number
  end: number
}

export interface ValidationIssue {
  leg_id: string | null
  field: string
  reason: string
  supported_value: string | null
  evidence: EvidenceSpan[]
}

export interface ValidationView {
  state: string
  status: string
  verdict: string | null
  code: string | null
  issues: ValidationIssue[]
  eligible_for_confirmation: boolean
  next_action: string
  model?: string | null
  prompt_hash?: string | null
  trace_id?: string | null
  attempts_remaining?: number
}

export interface DraftClarify {
  status: 'clarify'
  question: { turn_id: string; text: string }
  conversation_revision: number
}

export interface DraftBundle {
  status: 'ready' | 'needs_correction'
  bundle_id: string
  bundle_hash: string
  conversation_revision: number
  legs: DraftLeg[]
  total_debit: string
  validation: ValidationView | null
}

export interface DraftError {
  status: 'error'
  code: string
}

export type DraftResult = DraftClarify | DraftBundle | DraftError

// ── Signing envelopes and authorizations ─────────────────────────────

export interface EnvelopeLeg {
  leg_id: string
  action: string
  source_account_id: string
  target_type: string
  target_id: string
  display_metadata: { display_name: string; masked_account_suffix: string | null }
  amount: string
  currency: string
  reference: string | null
  equity_terms: EquityTerms | null
}

export interface BundlePayload {
  bundle_id: string
  revision: number
  owner_user_id: string
  conversation_id: string
  conversation_revision: number
  conversation_hash: string
  created_at: string
  legs: EnvelopeLeg[]
}

export interface MandatePayload {
  per_leg_limit: string | null
  daily_limit: string | null
  paused: boolean
  allowed_actions: string[]
}

export interface SigningEnvelope<P> {
  schema_version: number
  operation: string
  user_id: string
  credential_id: string
  operation_id: string
  payload: P
  conversation_id?: string | null
  conversation_revision?: number | null
  conversation_hash?: string | null
  active_mandate_version: number
  nonce: string
  issued_at: string
  expires_at: string
}

export interface AuthorizationView {
  authorization_id: string
  status: string
  operation: string
  envelope_hash: string
  expires_at: string
  reason_codes: string[]
  envelope: SigningEnvelope<BundlePayload>
  webauthn_options: RequestOptionsJSON
}

export interface ReceiptLeg {
  leg_id: string
  leg_index: number
  action: string
  source_account_id: string
  target_type: string
  target_id: string
  display_name: string
  amount: string
  transaction_id: string
  credit_transaction_id: string | null
  settlement_ref: string | null
  quantity?: number
  unit_price?: string
  quote_id?: string
}

export interface PaymentReceipt {
  receipt_id: string
  authorization_id: string
  operation: string
  operation_id: string
  payload_hash: string
  envelope_hash: string
  committed_at: string
  step_up_verified?: boolean
  total_debit: string
  legs: ReceiptLeg[]
  simulated: boolean
}

export interface CommittedResult {
  authorization_id: string
  status: 'COMMITTED'
  receipt_id: string
  receipt: PaymentReceipt
  replayed?: boolean
}

export interface StatusResult {
  authorization_id: string
  status: 'AWAITING_STEP_UP' | 'BLOCKED' | 'CANCELLED'
  reason?: string
  reason_codes: string[]
}

export type ApproveResult = CommittedResult | StatusResult

export interface StepUpStatus {
  authorization_id: string
  authorization_status: string
  challenge_id: string
  status: string
  expires_at: string
  attempts_remaining: number
  deliveries: number
  channel: string
}

// ── Mandate ──────────────────────────────────────────────────────────

export interface MandateSettings {
  per_leg_limit: string
  daily_limit: string
  allowed_actions: string[]
  paused: boolean
}

export interface MandateChangeRow extends MandateSettings {
  change_id: string
  authorization_id: string
  direction: string
  status: string
  status_reason: string | null
  base_version: number
  resulting_version: number | null
  effective_at: string | null
  requested_at: string
}

export interface MandateView {
  simulated: boolean
  current: MandateSettings & {
    version: number
    status: string
    max_legs: number
    expires_at: string | null
    expired: boolean
    effective_at: string
  }
  pending: MandateChangeRow | null
  cooling_hours: number
  scope_note: string
  history: MandateChangeRow[]
}

export interface MandateChangeRequest {
  credential_id: string
  per_leg_limit?: string
  daily_limit?: string
  allowed_actions?: string[]
  paused?: boolean
}

export interface MandateChangeView {
  authorization_id: string
  status: string
  operation: string
  envelope_hash: string
  expires_at: string
  reason_codes: string[]
  envelope: SigningEnvelope<MandatePayload>
  webauthn_options: RequestOptionsJSON
  change: {
    direction: string | null
    applies?: 'immediately' | 'after_cooling'
    current?: MandateSettings
    requested?: MandateSettings | null
    cooling_hours: number
    error?: string
  }
  scope_note: string
  simulated: boolean
}

export interface MandateReceipt {
  receipt_id: string
  direction: string
  outcome: 'APPLIED' | 'PENDING_COOLING' | string
  effective_at: string | null
  previous?: MandateSettings
  settings?: MandateSettings
  scope_note?: string
}

export interface MandateApproveResult {
  authorization_id: string
  status: string
  receipt_id?: string
  receipt?: MandateReceipt
  reason_codes?: string[]
}

// ── Evidence ─────────────────────────────────────────────────────────

export interface EvidenceTurn {
  turn_id: string
  seq: number
  role: TurnRole
  text: string
  edits_turn_id: string | null
  answers_question_id: string | null
  created_at: string
  superseded: boolean
}

export interface EvidenceDraft {
  bundle_id: string
  bundle_hash: string
  status: string
  created_at: string
  legs: DraftLeg[]
  validation: {
    verdict: string | null
    status: string
    issues: ValidationIssue[]
    model: string | null
    prompt_hash: string | null
    trace_url: string | null
  } | null
}

export interface EvidenceAuthorization {
  authorization_id: string
  bundle_id: string
  status: string
  status_reason: string | null
  envelope_hash: string
  issued_at: string
  expires_at: string
  approval: { credential_id: string; verified_at: string; user_verified: boolean } | null
  step_up: { status: string; attempts: number } | null
  receipt: PaymentReceipt | null
}

export interface EvidenceView {
  conversation_id: string
  revision: number
  turns: EvidenceTurn[]
  drafts: EvidenceDraft[]
  authorizations: EvidenceAuthorization[]
  audit: {
    ok: boolean
    first_bad_seq: number | null
    events: { seq: number; ts: string; source: string; step: string; trace_id: string | null }[]
  }
  trace_urls: string[]
}

// ── Transport ────────────────────────────────────────────────────────

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly reasonCodes: string[]
  readonly body: Record<string, unknown> | null

  constructor(status: number, code: string, reasonCodes: string[], body: Record<string, unknown> | null) {
    super(code)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.reasonCodes = reasonCodes
    this.body = body
  }

  /**
   * No usable gateway answer: offline, connection reset, or a proxy error page without a
   * JSON body. The request may or may not have reached the gateway, so money-moving
   * callers must recover by reading state, never by assuming failure or success.
   */
  get network(): boolean {
    return this.status === 0 || (this.status >= 500 && this.body === null)
  }
}

let unauthorizedHandler: (() => void) | null = null

/** Called when an authenticated route answers 401 (session missing or expired). */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function codeOf(status: number, body: Record<string, unknown> | null): string {
  if (status === 401) return 'SESSION_REQUIRED'
  if (body) {
    if (typeof body.code === 'string') return body.code
    if (typeof body.detail === 'string') return body.detail
    const detail = asRecord(body.detail)
    if (detail && typeof detail.code === 'string') return detail.code
    if (Array.isArray(body.detail)) return 'INVALID_REQUEST'
  }
  return `HTTP_${status}`
}

interface RequestOptions {
  json?: unknown
  form?: FormData
  /** Do not treat a 401 as "session ended" (sign-in routes, the initial /auth/me probe). */
  quiet401?: boolean
}

async function request<T>(method: 'GET' | 'POST', path: string, opts: RequestOptions = {}): Promise<T> {
  const init: RequestInit = { method, credentials: 'same-origin', cache: 'no-store' }
  if (opts.form) {
    init.body = opts.form
  } else if (opts.json !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(opts.json)
  }
  let res: Response
  try {
    res = await fetch(`/api${path}`, init)
  } catch {
    throw new ApiError(0, 'NETWORK_ERROR', [], null)
  }
  const data: unknown = await res.json().catch(() => null)
  if (!res.ok) {
    const body = asRecord(data)
    const reasons = Array.isArray(body?.reason_codes)
      ? (body?.reason_codes as unknown[]).filter((c): c is string => typeof c === 'string')
      : []
    if (res.status === 401 && !opts.quiet401) unauthorizedHandler?.()
    throw new ApiError(res.status, codeOf(res.status, body), reasons, body)
  }
  return data as T
}

const enc = encodeURIComponent

export const api = {
  health: () => request<Health>('GET', '/health'),

  // Identity
  me: () => request<Me>('GET', '/auth/me', { quiet401: true }),
  enrollBegin: (code: string) =>
    request<RegistrationOptionsJSON>('POST', '/auth/enroll/begin', { json: { code }, quiet401: true }),
  enrollVerify: (credential: unknown) =>
    request<{ user_id: string; status: string }>('POST', '/auth/enroll/verify', {
      json: { credential },
      quiet401: true,
    }),
  loginBegin: () => request<RequestOptionsJSON>('POST', '/auth/login/begin', { quiet401: true }),
  loginVerify: (assertion: AssertionJSON) =>
    request<{ user_id: string }>('POST', '/auth/login/verify', { json: { assertion }, quiet401: true }),
  logout: () => request<{ status: string }>('POST', '/auth/logout', { quiet401: true }),

  accounts: () => request<Account[]>('GET', '/accounts'),

  // Conversation
  createConversation: () => request<Conversation>('POST', '/conversations', { json: {} }),
  conversation: (id: string) => request<Conversation>('GET', `/conversations/${enc(id)}`),
  appendText: (id: string, text: string) =>
    request<Turn>('POST', `/conversations/${enc(id)}/text`, { json: { text } }),
  editTranscript: (id: string, editsTurnId: string, text: string) =>
    request<Turn>('POST', `/conversations/${enc(id)}/transcript-edit`, {
      json: { edits_turn_id: editsTurnId, text },
    }),
  answer: (id: string, questionTurnId: string, text: string) =>
    request<Turn>('POST', `/conversations/${enc(id)}/answer`, {
      json: { answers_question_id: questionTurnId, text },
    }),
  // Voice upload lives in src/audio/api.ts (voice task): it sends the WAV and consent=true.
  draft: (id: string) => request<DraftResult>('POST', `/conversations/${enc(id)}/draft`, { json: {} }),
  evidence: (id: string) => request<EvidenceView>('GET', `/conversations/${enc(id)}/evidence`),
  validation: (bundleId: string) => request<ValidationView>('GET', `/drafts/${enc(bundleId)}/validation`),

  // Payment authorizations
  createAuthorization: (bundleId: string, credentialId: string) =>
    request<AuthorizationView>('POST', `/drafts/${enc(bundleId)}/authorizations`, {
      json: { credential_id: credentialId },
    }),
  authorization: (id: string) => request<AuthorizationView>('GET', `/authorizations/${enc(id)}`),
  approve: (id: string, expectedEnvelopeHash: string, assertion: AssertionJSON) =>
    request<ApproveResult>('POST', `/authorizations/${enc(id)}/approve`, {
      json: { authorization_id: id, expected_envelope_hash: expectedEnvelopeHash, assertion },
    }),
  receipt: (id: string) => request<CommittedResult>('GET', `/authorizations/${enc(id)}/receipt`),
  cancelAuthorization: (id: string) => request<StatusResult>('POST', `/authorizations/${enc(id)}/cancel`),

  // Simulated step-up
  stepUpStatus: (id: string) => request<StepUpStatus>('GET', `/authorizations/${enc(id)}/step-up`),
  stepUpSubmit: (id: string, code: string, expectedEnvelopeHash: string) =>
    request<ApproveResult>('POST', `/authorizations/${enc(id)}/step-up`, {
      json: { code, expected_envelope_hash: expectedEnvelopeHash },
    }),
  stepUpResend: (id: string) => request<StepUpStatus>('POST', `/authorizations/${enc(id)}/step-up/resend`),

  // Mandate
  mandate: () => request<MandateView>('GET', '/mandate'),
  requestMandateChange: (body: MandateChangeRequest) =>
    request<MandateChangeView>('POST', '/mandate/changes', { json: body }),
  mandateChange: (id: string) => request<MandateChangeView>('GET', `/mandate/changes/${enc(id)}`),
  approveMandateChange: (id: string, expectedEnvelopeHash: string, assertion: AssertionJSON) =>
    request<MandateApproveResult>('POST', `/mandate/changes/${enc(id)}/approve`, {
      json: { authorization_id: id, expected_envelope_hash: expectedEnvelopeHash, assertion },
    }),
}

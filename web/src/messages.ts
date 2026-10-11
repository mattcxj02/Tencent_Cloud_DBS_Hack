// The one place that turns the gateway's stable codes into plain English.
// The UI keys on codes, never on server prose. Unknown codes are shown as the code
// itself so nothing is silently hidden or reworded into something misleading.

import { VOICE_CODE_MESSAGES } from './audio/messages'

export const CODE_MESSAGES: Record<string, string> = {
  // Voice capture and upload codes are owned by the voice task (src/audio/messages.ts).
  ...VOICE_CODE_MESSAGES,

  // ── Hard policy: BLOCK (no override by any model or code) ──
  UNSUPPORTED_OPERATION: 'This kind of request cannot be paid through the assistant.',
  KYC_NOT_VERIFIED: 'Your identity verification is missing or not complete.',
  KYC_EXPIRED: 'Your identity verification has expired. Please renew it first.',
  KYC_TIER_DAILY_LIMIT: "This would take you over today's limit for your verification level.",
  NO_ACTIVE_MANDATE: 'There is no active spending mandate for the assistant.',
  MANDATE_PAUSED: 'Payments through this assistant are paused. Resume them on the Mandate page.',
  MANDATE_EXPIRED: 'Your spending mandate for the assistant has expired.',
  MANDATE_ACTION_NOT_ALLOWED: 'Your mandate does not allow this type of payment through the assistant.',
  MANDATE_MAX_LEGS: 'This request has more payments than your mandate allows at once.',
  MANDATE_PER_LEG_LIMIT: "One of these payments is above your mandate's per-payment limit.",
  MANDATE_DAILY_LIMIT: "This would take you over your mandate's daily limit.",
  SOURCE_ACCOUNT_NOT_OWNED: 'One of the accounts to pay from is not yours.',
  TARGET_ACCOUNT_NOT_OWNED: 'One of the destination accounts is not yours.',
  SAME_ACCOUNT_TRANSFER: 'A transfer cannot go to the same account it comes from.',
  PAYEE_NOT_SAVED: 'The recipient is not one of your saved payees.',
  PAYEE_SANCTIONED: 'This recipient cannot be paid (simulated sanctions check).',
  BILLER_NOT_SAVED: 'This biller is not saved on your profile.',
  CARD_NOT_OWNED: 'This card is not yours.',
  CARD_OVERPAYMENT: 'The card payment is more than the amount outstanding on the card.',
  QUOTE_NOT_FOUND: 'The share price quote could not be found. Please ask again for a new draft.',
  QUOTE_EXPIRED: 'The share price quote has expired. Please ask again for a fresh price.',
  QUOTE_SYMBOL_MISMATCH: 'The share price quote is for a different stock.',
  QUOTE_TERMS_CHANGED: 'The share price no longer matches what you reviewed.',
  INSUFFICIENT_FUNDS: 'There is not enough money in the account for one of these payments.',
  VELOCITY_LIMIT: 'Too many transfers in a short time (more than five in ten minutes). Please wait a few minutes.',

  // ── Hard policy: extra check (step-up) ──
  RISK_THRESHOLD_EXCEEDED: 'The total is above the extra-check threshold (SGD 500.00 in this prototype), so a one-time code is needed.',
  SCAM_SIGNAL_SAFE_ACCOUNT:
    'You mentioned a "safe account". Banks never ask you to move money to a safe account.',
  SCAM_SIGNAL_IMPERSONATION:
    'It sounds like someone claiming to be an official told you to pay. Banks and police never do this.',
  SCAM_SIGNAL_SECRECY: 'You were asked to keep this secret. That is a common scam sign.',
  SCAM_SIGNAL_REMOTE_ACCESS:
    'Remote-access software was mentioned. Never let anyone control your device for a payment.',
  SCAM_SIGNAL_URGENCY: 'There is pressure to act urgently, together with another scam sign.',

  // ── Approval lifecycle ──
  AUTHORIZATION_NOT_FOUND: 'This approval could not be found.',
  AUTHORIZATION_EXPIRED: 'The approval window (2 minutes) has closed. Nothing was paid.',
  AUTHORIZATION_SUPERSEDED:
    'Your conversation or mandate changed after this was prepared, so it can no longer be approved.',
  CONVERSATION_OR_MANDATE_CHANGED:
    'Your conversation or mandate changed after this was prepared, so it can no longer be approved.',
  ALREADY_APPROVED:
    'This approval was already used. Please get a fresh confirmation to try again.',
  NOT_PENDING: 'This approval is no longer waiting for you.',
  IDEMPOTENCY_CONFLICT: 'A different payment was already made under this request.',
  OPERATION_MISMATCH: 'This approval is for a different kind of request.',
  ENVELOPE_HASH_MISMATCH: 'The details on screen do not match the stored request. Please reload it.',
  ENVELOPE_INTEGRITY: 'The stored request failed its integrity check. Nothing was paid.',
  SIGNATURE_INVALID: 'Your passkey signature could not be verified. Nothing was approved.',
  CREDENTIAL_NOT_OWNED: 'That passkey is not registered to you. Sign in again on this device.',
  POLICY_BLOCK: 'This cannot go ahead under the bank rules.',
  COMMIT_FAILED: 'The simulated bank could not complete it and rolled everything back. Nothing was paid.',
  COMMIT_NOT_IMPLEMENTED: 'Payments are not switched on in this build. Nothing was paid.',
  NOT_COMMITTED: 'This has not been paid.',
  USER_CANCELLED: 'You cancelled this. Nothing was paid.',
  AUTHORIZATION_ID_MISMATCH: 'The request was malformed. Please try again.',

  // ── Drafts and validation ──
  DRAFT_NOT_FOUND: 'This draft could not be found.',
  DRAFT_NOT_ELIGIBLE: 'This draft can no longer be approved.',
  DRAFT_STALE_REVISION: 'The conversation changed after this draft was prepared. A new draft is needed.',
  SEMANTIC_VALIDATION_REQUIRED: 'The independent check has not confirmed this draft yet.',
  VALIDATION_MISMATCH: 'The independent check found that this draft does not match what you said.',
  VALIDATION_NOT_FOUND: 'The independent check has not run for this draft.',
  VALIDATION_FINAL: 'The independent check already gave its final answer for this draft.',
  VALIDATION_IN_PROGRESS: 'The independent check is still running.',
  VALIDATION_ATTEMPTS_EXHAUSTED: 'The independent check failed too many times. Please restate your request.',
  VALIDATION_RATE_LIMITED: 'Too many checks in a short time. Please wait a moment.',
  CONVERSATION_INTEGRITY: 'The conversation record failed its integrity check.',
  BUNDLE_INTEGRITY: 'The draft failed its integrity check.',
  BANK_METADATA_UNAVAILABLE: 'Bank details needed for the check are unavailable right now.',
  VALIDATION_REQUEST_INVALID: 'The independent check could not be prepared.',
  VALIDATOR_TIMEOUT: 'The independent check took too long.',
  VALIDATOR_UNAVAILABLE: 'The independent check is unavailable right now.',
  VALIDATOR_HTTP_ERROR: 'The independent check failed.',
  VALIDATOR_RESPONSE_INVALID: 'The independent check returned an unusable answer, so this cannot be approved.',
  LLM_TIMEOUT: 'The independent check took too long.',
  LLM_NOT_CONFIGURED: 'The independent check is not configured.',
  LLM_PROVIDER_ERROR: 'The independent check service had an error.',
  OUTPUT_NOT_JSON: 'The independent check returned an unusable answer, so this cannot be approved.',
  OUTPUT_SCHEMA_INVALID: 'The independent check returned an unusable answer, so this cannot be approved.',
  CONTEXT_LIMIT_RESTATE:
    'This conversation is too long to check safely. Please start a new conversation and say the whole request in one message.',
  EXTRACTOR_UNAVAILABLE: 'The assistant that prepares drafts is unavailable right now.',
  AGENT_INTERNAL_ERROR: 'The assistant that prepares drafts had an internal error.',
  AGENT_UNAVAILABLE: 'The assistant that prepares drafts is unavailable right now.',
  AGENT_TIMEOUT: 'The assistant took too long to prepare a draft.',
  AGENT_BAD_RESPONSE: 'The assistant returned something unusable, so nothing was drafted.',
  AGENT_ERROR: 'The assistant could not prepare a draft.',
  DRAFT_FAILED: 'Something went wrong while preparing the draft. Nothing was paid.',
  ORCHESTRATION_TIMEOUT: 'Preparing and checking the draft took too long. Please try again.',
  CONVERSATION_CHANGED: 'The conversation changed while the draft was being prepared. Please try again.',
  CONVERSATION_NOT_FOUND: 'This conversation could not be found. Please start a new one.',
  STALE_CONVERSATION_REVISION: 'The conversation changed while the draft was being prepared. Please try again.',
  VALIDATION_NOT_ELIGIBLE: 'Something changed since the independent check (for example a balance). Please try again.',
  VALIDATION_ERROR: 'The independent check failed, so this cannot be approved yet. Please try again.',
  UNEXPECTED_DRAFT_STATUS: 'The draft response was not understood, so nothing can be approved.',

  // ── Simulated step-up ──
  STEP_UP_CODE_INVALID: 'That code is not right.',
  STEP_UP_ATTEMPTS_EXHAUSTED: 'Too many wrong codes, so this payment has been blocked. Nothing was paid.',
  STEP_UP_EXPIRED: 'The code expired together with the approval window. Nothing was paid.',
  STEP_UP_BINDING_MISMATCH: 'The code does not belong to this payment, so it has been blocked.',
  STEP_UP_NOT_ISSUED: 'No code was issued for this payment.',
  STEP_UP_NOT_ACTIVE: 'This code can no longer be used.',
  STEP_UP_NOT_REQUIRED: 'This payment is not waiting for a code.',
  STEP_UP_CODE_UNAVAILABLE: 'The simulated phone channel cannot re-send this code.',

  // ── Mandate changes ──
  MANDATE_NOT_FOUND: 'There is no mandate to manage.',
  MANDATE_NO_CHANGE: 'That does not change any setting.',
  MANDATE_CHANGE_MIXED:
    'Please change one direction at a time: submit decreases (or pause) and increases (or resume) separately.',
  MANDATE_LIMIT_INVALID: 'Limits must be positive SGD amounts with at most two decimals.',
  MANDATE_LIMIT_ABOVE_MAXIMUM: 'A limit cannot be raised above SGD 20,000.00.',
  MANDATE_PER_LEG_ABOVE_DAILY: 'The per-payment limit cannot be higher than the daily limit.',
  MANDATE_ACTIONS_DUPLICATE: 'Each payment type can only be listed once.',
  MANDATE_VERSION_CHANGED: 'Your mandate changed after this request. Please make the request again.',
  MANDATE_TIGHTENED: 'A decrease or pause replaced this pending approval.',
  MANDATE_ACTIVATED: 'A cooled mandate change just took effect. Please approve again.',
  MANDATE_CHANGE_REPLACED: 'A newer pending increase replaced this one.',
  MANDATE_CHANGE_CANCELLED_BY_TIGHTENING: 'A decrease or pause cancelled this pending increase.',

  // ── Session and transport ──
  SESSION_REQUIRED: 'Your session has ended. Please sign in again.',
  NETWORK_ERROR: "We couldn't reach the bank service. Check your connection and try again.",
  INVALID_REQUEST: 'The request was not accepted. Please try again.',
  HTTP_500: 'The bank service had an internal error.',
  HTTP_502: "The bank service didn't respond.",
  HTTP_503: 'The service is temporarily unavailable.',
  HTTP_504: 'The service took too long to respond.',
  HTTP_404: 'This feature is not available on the gateway yet.',
  HTTP_429: 'Too many attempts. Please wait a moment and try again.',
  'origin not allowed':
    'The bank rejected this page address (Origin check). Open the app at its configured address.',
  'rate limit exceeded': 'Too many attempts. Please wait a moment and try again.',
  'not found': 'That item could not be found.',
}

/** "NOT_PENDING: COMMITTED" → { base: "NOT_PENDING", detail: "COMMITTED" }. */
export function splitCode(code: string): { base: string; detail: string | null } {
  const i = code.indexOf(': ')
  return i === -1 ? { base: code, detail: null } : { base: code.slice(0, i), detail: code.slice(i + 2) }
}

const CODE_LIKE = /^[A-Z][A-Z0-9_]*(: .*)?$/

/**
 * Plain-English text for one code. An unknown code is shown as the code itself; a
 * non-code `detail` (short gateway prose such as "invalid enrollment code") is shown
 * as plain text. Either way React escapes it.
 */
export function describeCode(code: string): string {
  if (CODE_MESSAGES[code]) return CODE_MESSAGES[code]
  const { base, detail } = splitCode(code)
  if (CODE_MESSAGES[base]) return detail ? `${CODE_MESSAGES[base]} (status: ${detail})` : CODE_MESSAGES[base]
  if (!CODE_LIKE.test(code)) return code
  return `Something went wrong (code: ${code}).`
}

/** Distinct messages for a list of reason codes, in order. */
export function describeCodes(codes: string[]): { code: string; message: string }[] {
  return [...new Set(codes)].map((code) => ({ code, message: describeCode(code) }))
}

/** Authorization statuses as words. */
export const STATUS_LABEL: Record<string, string> = {
  PENDING_APPROVAL: 'Waiting for your approval',
  AWAITING_STEP_UP: 'Waiting for your one-time code',
  COMMITTED: 'Paid (simulated)',
  BLOCKED: 'Blocked — nothing paid',
  EXPIRED: 'Expired — nothing paid',
  SUPERSEDED: 'Replaced by a newer version — nothing paid',
  CANCELLED: 'Cancelled — nothing paid',
}

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status
}

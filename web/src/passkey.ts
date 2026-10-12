// Passkey (WebAuthn) signing. The options always come from the gateway and carry the
// challenge derived from the stored signing envelope; the browser never builds a
// challenge or reuses a login challenge for an approval.
import { browserSupportsWebAuthn, startAuthentication } from '@simplewebauthn/browser'
import type { AssertionJSON, RequestOptionsJSON } from './api'

export type PasskeyFailure = 'denied' | 'unsupported' | 'security' | 'failed'

export class PasskeyError extends Error {
  readonly kind: PasskeyFailure
  constructor(kind: PasskeyFailure, message: string) {
    super(message)
    this.name = 'PasskeyError'
    this.kind = kind
  }
}

export const PASSKEY_MESSAGES: Record<PasskeyFailure, string> = {
  denied:
    'The passkey prompt (Touch ID / Windows Hello) was cancelled, timed out, or this device has no passkey for your account. Nothing was approved. You can try again.',
  unsupported: 'This browser cannot use passkeys. Please use a recent Chrome, Safari or Edge.',
  security:
    'The browser refused the passkey request for this page address. Open the app at its configured address (for example http://localhost:5173).',
  failed: 'The passkey could not sign. Nothing was approved. Please try again.',
}

export async function signWithPasskey(options: RequestOptionsJSON): Promise<AssertionJSON> {
  if (!browserSupportsWebAuthn()) throw new PasskeyError('unsupported', PASSKEY_MESSAGES.unsupported)
  try {
    return await startAuthentication({ optionsJSON: options })
  } catch (e) {
    const name = e instanceof Error ? e.name : ''
    if (name === 'NotAllowedError' || name === 'AbortError') {
      throw new PasskeyError('denied', PASSKEY_MESSAGES.denied)
    }
    if (name === 'SecurityError') throw new PasskeyError('security', PASSKEY_MESSAGES.security)
    throw new PasskeyError('failed', PASSKEY_MESSAGES.failed)
  }
}

import { useEffect, useState } from 'react'
import { api, ApiError, type DraftBundle } from '../api'
import { sumCents, centsToString } from '../money'
import { Confirmation } from './Confirmation'
import { Button, Notice } from './ui'

/** Reload always reads the old authorization; it never issues a replacement payment. */
export function ExistingPayment({ authorizationId, onCommitted, onFinished, onEdit, onRedraft, onClose }: {
  authorizationId: string; onCommitted: () => void; onFinished: () => void
  onEdit: () => void; onRedraft: () => void; onClose: () => void
}) {
  const [draft, setDraft] = useState<DraftBundle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    const load = async () => {
      setError(null)
      const view = await api.authorization(authorizationId)
      const payload = view.envelope.payload
      const validation = view.status === 'PENDING_APPROVAL'
        ? await api.validation(payload.bundle_id).catch(() => null) : null
      if (!active) return
      setDraft({
        status: 'ready', bundle_id: payload.bundle_id, bundle_hash: '',
        conversation_revision: payload.conversation_revision,
        legs: payload.legs.map((l) => ({ ...l, display_name: l.display_metadata.display_name,
          masked_account_suffix: l.display_metadata.masked_account_suffix, source_display: 'Account' })),
        total_debit: centsToString(sumCents(payload.legs.map((l) => l.amount))), validation,
      })
    }
    void load().catch((e) => { if (active) setError(e instanceof ApiError ? e.code : 'NETWORK_ERROR') })
    return () => { active = false }
  }, [authorizationId, attempt])
  if (!draft) return <Notice kind="info" title="Checking your existing payment">
    {error && <><p>Its result is not confirmed yet. Check this approval before starting another payment.</p>
      <Button onClick={() => setAttempt((a) => a + 1)}>Check existing approval</Button></>}
  </Notice>
  return <Confirmation draft={draft} existingAuthorizationId={authorizationId} prevLegs={null}
    onEdit={onEdit} onRedraft={onRedraft} onClose={onClose}
    onCommitted={onCommitted} onFinished={onFinished} />
}

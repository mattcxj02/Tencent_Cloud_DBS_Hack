// The gateway-captured conversation. Each original turn is shown once, at its position,
// as its latest correction (same rule the agent uses); earlier versions stay visible
// behind a disclosure. Any user turn can be corrected, which creates a new revision.
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import type { Turn } from '../api'
import { Badge, Button, inputClass } from './ui'

const USER_ROLES = new Set(['user_text', 'user_asr_final', 'user_transcript_edit', 'user_answer'])

export interface VisibleTurn {
  /** The latest version (what the assistant reads now). */
  current: Turn
  /** All versions, oldest first (length > 1 means it was corrected). */
  versions: Turn[]
  /** How it was first captured (text, voice, answer). */
  origin: Turn
}

export function visibleTurns(turns: Turn[]): VisibleTurn[] {
  const sorted = [...turns].sort((a, b) => a.seq - b.seq)
  const byId = new Map(sorted.map((t) => [t.turn_id, t]))
  const latestEdit = new Map<string, Turn>()
  for (const t of sorted) if (t.edits_turn_id) latestEdit.set(t.edits_turn_id, t)
  const out: VisibleTurn[] = []
  for (const t of sorted) {
    if (t.edits_turn_id && byId.has(t.edits_turn_id)) continue // shown at the original's position
    const versions = [t]
    const seen = new Set<string>()
    let final = t
    while (latestEdit.has(final.turn_id) && !seen.has(final.turn_id)) {
      seen.add(final.turn_id)
      final = latestEdit.get(final.turn_id)!
      versions.push(final)
    }
    if (USER_ROLES.has(final.role) || final.role === 'app_question') out.push({ current: final, versions, origin: t })
  }
  return out
}

function speakerLabel(v: VisibleTurn): string {
  if (v.current.role === 'app_question') return 'Assistant asked'
  if (v.origin.role === 'user_asr_final') return 'You said (voice transcript)'
  if (v.origin.role === 'user_answer') return 'Your answer'
  return 'You wrote'
}

function TurnEditor({
  initial,
  onSave,
  onCancel,
  busy,
}: {
  initial: string
  onSave: (text: string) => void
  onCancel: () => void
  busy: boolean
}) {
  const [text, setText] = useState(initial)
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    ref.current?.focus()
    ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [])
  const save = () => {
    const t = text.trim()
    if (!t || t === initial.trim()) return onCancel()
    onSave(t)
  }
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') onCancel()
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save()
  }
  return (
    <div className="mt-2 space-y-2">
      <label htmlFor="turn-editor" className="block text-sm font-semibold">
        Correct what you said
      </label>
      <textarea
        id="turn-editor"
        ref={ref}
        value={text}
        maxLength={2000}
        rows={3}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        className={inputClass}
        aria-describedby="turn-editor-hint"
      />
      <p id="turn-editor-hint" className="text-sm text-slate-700">
        Check names and amounts. The original stays in the record. Saving replaces any draft in progress and prepares the corrected draft. Esc cancels the edit.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={save} busy={busy} busyLabel="Saving…">
          Save correction
        </Button>
        <Button variant="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

export function Transcript({
  turns,
  editingTurnId,
  busy,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
}: {
  turns: VisibleTurn[]
  editingTurnId: string | null
  busy: boolean
  onStartEdit: (turnId: string) => void
  onCancelEdit: () => void
  onSaveEdit: (turnId: string, text: string) => void
}) {
  if (turns.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-400 bg-white p-4 text-base text-slate-700">
        <p className="font-semibold text-slate-900">Tell me what to pay, in your own words.</p>
        <p className="mt-1">
          For example: “Send John 50 dollars from savings and pay off my credit card”, or “Buy 500 dollars of DBS
          shares”.
        </p>
      </div>
    )
  }
  return (
    <ol className="space-y-3" aria-label="Conversation">
      {turns.map((v) => {
        const isUser = v.current.role !== 'app_question'
        const edited = v.versions.length > 1
        const editing = editingTurnId === v.current.turn_id
        return (
          <li key={v.origin.turn_id} className={isUser ? 'flex justify-end' : 'flex justify-start'}>
            <div
              className={`max-w-[90%] rounded-2xl border px-4 py-2 sm:max-w-[80%] ${
                isUser ? 'border-indigo-300 bg-indigo-50' : 'border-slate-300 bg-white'
              }`}
            >
              <p className="text-sm font-semibold text-slate-700">
                {speakerLabel(v)} {edited && <Badge tone="changed">Corrected</Badge>}
              </p>
              <p className="whitespace-pre-wrap break-words text-base text-slate-950">{v.current.text}</p>
              {edited && (
                <details className="mt-1 text-sm text-slate-700">
                  <summary className="cursor-pointer">Earlier versions ({v.versions.length - 1})</summary>
                  <ol className="mt-1 space-y-1">
                    {v.versions.slice(0, -1).map((old) => (
                      <li key={old.turn_id}>
                        <span className="sr-only">Replaced: </span>
                        <s>{old.text}</s>
                      </li>
                    ))}
                  </ol>
                </details>
              )}
              {isUser &&
                (editing ? (
                  <TurnEditor
                    initial={v.current.text}
                    busy={busy}
                    onCancel={onCancelEdit}
                    onSave={(text) => onSaveEdit(v.current.turn_id, text)}
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => onStartEdit(v.current.turn_id)}
                    disabled={busy}
                    className="mt-1 min-h-[44px] px-1 text-sm font-semibold text-indigo-800 underline underline-offset-2 disabled:opacity-60"
                    aria-label={`Correct this: ${v.current.text.slice(0, 60)}`}
                  >
                    {v.origin.role === 'user_asr_final' ? 'Correct this transcript' : 'Correct this'}
                  </button>
                ))}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

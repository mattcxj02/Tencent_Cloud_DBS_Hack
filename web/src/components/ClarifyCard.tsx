// One short clarification question. If it ends with a list of options
// ("Which John do you mean: John Doe (account ending 4521) or John Tan (...)?") the
// options become chips; tapping one sends it as the answer. Typing still works.
// A "Did you mean …?" or "I couldn't find …" question (a near spelling such as "mom" for
// "Mum", or a name that matches no one) also offers to remember the customer's words as a
// nickname for the payee they pick. Account choices show the live available balance.
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { ClarificationQuestion, Payee, StructuredAnswer } from '../api'
import { describeCode } from '../messages'
import { formatMoney } from '../money'
import { useAnnounce } from '../session'
import { Button, Card, Notice } from './ui'

type Choice = NonNullable<ClarificationQuestion['clarification']>['fields'][number]['choices'][number]

const MAX_OPTIONS = 6
const MAX_OPTION_LEN = 80
/** Must match agent/questions.py (recipient_did_you_mean and recipient_not_found). */
const UNKNOWN_NAME = /^I couldn't find “([^”…]+)” (?:exactly\. Did you mean: |among your saved payees)/

/** Options after the last ": " of a question ending in "?", joined by ", " and " or ". */
export function choiceOptions(question: string): string[] {
  const text = question.trim()
  if (!text.endsWith('?')) return []
  // A grouped question has one line per payment; its last list answers only the last line.
  if (text.includes('\n')) return []
  const colon = text.lastIndexOf(': ')
  if (colon === -1) return []
  const list = text.slice(colon + 2, -1)
  const parts = list.split(/, | or /).map((p) => p.trim()).filter(Boolean)
  if (parts.length < 2 || parts.length > MAX_OPTIONS) return []
  if (parts.some((p) => p.length > MAX_OPTION_LEN)) return []
  return parts
}

/** The customer's words in a "Did you mean …?" or "I couldn't find …" question, else null. */
export function suggestedWords(question: string): string | null {
  return UNKNOWN_NAME.exec(question.trim())?.[1] ?? null
}

/** A choice's label, plus the account's available balance when the gateway sent one. */
function ChoiceText({ label, choice }: { label: string; choice?: Choice }) {
  if (!choice?.available) return <>{label}</>
  const money = formatMoney(choice.available)
  return (
    <span className="flex flex-col items-start text-left">
      <span>{label}</span>
      <span className="text-sm font-normal">
        {choice.insufficient ? `${money} available, may not cover this payment` : `${money} available`}
      </span>
    </span>
  )
}

/** The agent's label for a saved payee (agent/names.py payee_record). */
function payeeLabel(p: Payee): string {
  if (p.kind === 'paynow') return `${p.display_name} (PayNow)`
  return p.masked_suffix ? `${p.display_name} (account ending ${p.masked_suffix})` : p.display_name
}

export function ClarifyCard({
  question,
  disabled,
  onChoose,
}: {
  question: ClarificationQuestion
  disabled: boolean
  onChoose: (answer: string | StructuredAnswer) => void
}) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const announce = useAnnounce()
  useEffect(() => headingRef.current?.focus(), [question.turn_id])
  const fields = question.clarification?.fields ?? []
  // "Payment 2: Pay from" only helps when the choices are about more than one payment.
  const severalPayments = new Set(fields.map((f) => f.leg_id)).size > 1
  const options = fields.length === 1 ? fields[0].choices.map((c) => c.label) : choiceOptions(question.text)
  const words = suggestedWords(question.text)

  const [payees, setPayees] = useState<Payee[] | null>(null)
  const [remember, setRemember] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Record<string, string>>({})
  useEffect(() => {
    setSelected({})
    setRemember(false)
    setError(null)
    if (!words) return
    let alive = true
    api.payees().then(
      (list) => alive && setPayees(list),
      () => alive && setPayees(null), // the offer is optional; answering still works
    )
    return () => {
      alive = false
    }
  }, [question.turn_id, words])

  const payeeFor = (option: string) => payees?.find((p) => payeeLabel(p) === option) ?? null
  const canRemember = words !== null && fields.length <= 1
    && new Set(options).size === options.length && options.some((o) => payeeFor(o) !== null)

  const choose = async (option: string, choiceId?: string) => {
    const payee = payeeFor(option)
    if (remember && words && payee) {
      setSaving(true)
      setError(null)
      try {
        await api.addNickname(payee.payee_id, words)
        announce(`Saved “${words}” as a nickname for ${payee.display_name}.`)
      } catch (err) {
        setRemember(false)
        setError(
          `${err instanceof ApiError ? describeCode(err.code) : 'The nickname could not be saved.'} Tap your answer again to continue without it.`,
        )
        return
      } finally {
        setSaving(false)
      }
    }
    if (fields.length === 1 && question.clarification) {
      const f = fields[0]
      const choice = f.choices.find((c) => c.choice_id === choiceId)
      if (!choice) return
      onChoose({ question_revision: question.clarification.revision, selections: [
        { field_id: f.field_id, leg_id: f.leg_id, field: f.field, choice_id: choice.choice_id },
      ] })
    } else onChoose(option)
  }

  return (
    <Card title="A few details before we draft" titleId="clarify-heading" headingRef={headingRef}>
      <p className="whitespace-pre-line text-lg text-slate-900">{question.text}</p>
      {fields.length > 1 && fields.map((f) => (
        <fieldset key={f.field_id} className="mt-3">
          <legend className="font-medium">
            {severalPayments && `Payment ${f.leg_id.slice(4)}: `}{f.field === 'target' ? 'Recipient' : 'Pay from'}
          </legend>
          <div className="mt-2 flex flex-wrap gap-2">
            {f.choices.map((c) => (
              <Button key={c.choice_id} variant="secondary" disabled={disabled || saving}
                aria-pressed={selected[f.field_id] === c.choice_id}
                onClick={() => setSelected((prev) => ({ ...prev, [f.field_id]: c.choice_id }))}>
                {selected[f.field_id] === c.choice_id && <span aria-hidden="true">✓</span>}
                <ChoiceText label={c.label} choice={c} />
              </Button>
            ))}
          </div>
        </fieldset>
      ))}
      {fields.length > 1 && (
        <Button className="mt-3" disabled={disabled || fields.some((f) => !selected[f.field_id])}
          onClick={() => onChoose({ question_revision: question.clarification!.revision, selections: fields.map((f) => ({
            field_id: f.field_id, leg_id: f.leg_id, field: f.field, choice_id: selected[f.field_id],
          })) })}>
          Use these choices
        </Button>
      )}
      {fields.length <= 1 && options.length > 0 && (
        <div role="group" aria-label="Choose an answer" className="mt-3 flex flex-wrap gap-2">
          {options.map((opt, index) => {
            const choice = fields[0]?.choices[index]
            return (
              <Button
                key={choice?.choice_id ?? opt}
                variant="secondary"
                disabled={disabled || saving}
                onClick={() => void choose(opt, choice?.choice_id)}
                className={choice?.available ? '' : 'rounded-full'}
              >
                <ChoiceText label={opt} choice={choice} />
              </Button>
            )
          })}
        </div>
      )}
      {fields.length > 0 && (
        <Button className="mt-3" variant="secondary" disabled={disabled || saving}
          onClick={() => onChoose('None of these. I want to choose something else.')}>
          None of these
        </Button>
      )}
      {canRemember && (
        <label className="mt-3 flex min-h-[44px] items-center gap-2 text-base text-slate-900">
          <input
            type="checkbox"
            className="h-5 w-5"
            checked={remember}
            disabled={disabled || saving}
            onChange={(e) => setRemember(e.target.checked)}
          />
          Also remember “{words}” as a nickname for the payee I pick (saved payees only)
        </label>
      )}
      {error && (
        <div className="mt-3">
          <Notice kind="error" title={error} focus />
        </div>
      )}
      <p className="mt-3 text-sm text-slate-700">
        {options.length > 0 ? 'Or type your answer below.' : 'Type or say your answer below.'}
      </p>
    </Card>
  )
}

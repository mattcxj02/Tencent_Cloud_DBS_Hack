// One short clarification question. If it ends with a list of options
// ("Which John do you mean: John Doe (account ending 4521) or John Tan (...)?") the
// options become chips; tapping one sends it as the answer. Typing still works.
import { useEffect, useRef } from 'react'
import { Button, Card } from './ui'

const MAX_OPTIONS = 6
const MAX_OPTION_LEN = 80

/** Options after the last ": " of a question ending in "?", joined by ", " and " or ". */
export function choiceOptions(question: string): string[] {
  const text = question.trim()
  if (!text.endsWith('?')) return []
  const colon = text.lastIndexOf(': ')
  if (colon === -1) return []
  const list = text.slice(colon + 2, -1)
  const parts = list.split(/, | or /).map((p) => p.trim()).filter(Boolean)
  if (parts.length < 2 || parts.length > MAX_OPTIONS) return []
  if (parts.some((p) => p.length > MAX_OPTION_LEN)) return []
  return parts
}

export function ClarifyCard({
  question,
  disabled,
  onChoose,
}: {
  question: { turn_id: string; text: string }
  disabled: boolean
  onChoose: (answer: string) => void
}) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [question.turn_id])
  const options = choiceOptions(question.text)
  return (
    <Card title="One question before we draft" titleId="clarify-heading" headingRef={headingRef}>
      <p className="text-lg text-slate-900">{question.text}</p>
      {options.length > 0 && (
        <div role="group" aria-label="Choose an answer" className="mt-3 flex flex-wrap gap-2">
          {options.map((opt) => (
            <Button key={opt} variant="secondary" disabled={disabled} onClick={() => onChoose(opt)} className="rounded-full">
              {opt}
            </Button>
          ))}
        </div>
      )}
      <p className="mt-3 text-sm text-slate-700">
        {options.length > 0 ? 'Or type your answer below.' : 'Type or say your answer below.'}
      </p>
    </Card>
  )
}

// Browser-only regression fixture. Uses React's installed act helper; no gateway calls.
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { api } from '../src/api'
import type { ClarificationQuestion, StructuredAnswer } from '../src/api'
import { ClarifyCard } from '../src/components/ClarifyCard'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
const host = document.getElementById('fixture')!
const results = document.getElementById('results')!
const root = createRoot(host)
const answers: (string | StructuredAnswer)[] = []
const requests: Record<string, unknown>[] = []
window.fetch = async (_input, init) => {
  requests.push(JSON.parse(String(init?.body)))
  return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
}

const question: ClarificationQuestion = {
  turn_id: 'question-1', text: 'Who receives payment 1? Which account pays?',
  clarification: { revision: 7, fields: [
    { field_id: 'target-1', leg_id: 'leg-1', field: 'target', prompt: 'Who?', choices: [
      { choice_id: 'mum', label: 'Mum (account ending 9988)' },
      { choice_id: 'ahma', label: 'Ah Ma (account ending 7766)' },
    ] },
    { field_id: 'source-1', leg_id: 'leg-1', field: 'source', prompt: 'Pay from?', choices: [
      { choice_id: 'savings', label: 'Savings (account ending 1234)' },
      { choice_id: 'current', label: 'Current (account ending 5678)' },
    ] },
  ] },
}

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function button(label: string): HTMLButtonElement {
  const element = [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)
  check(element, `Missing button: ${label}`)
  return element
}

async function render(q: ClarificationQuestion, disabled = false) {
  await act(async () => root.render(<ClarifyCard question={q} disabled={disabled}
    onChoose={(answer) => { answers.push(answer) }} />))
}

async function runTests() {
  const passed: string[] = []
  await render(question)
  check(button('Use these choices').disabled, 'Incomplete group must not submit')
  await act(async () => button('Mum (account ending 9988)').click())
  check(button('Use these choices').disabled && answers.length === 0, 'First choice must wait for source')
  await act(async () => button('Current (account ending 5678)').click())
  check(!button('Use these choices').disabled, 'Complete group must allow submission')
  await act(async () => button('Use these choices').click())
  const selected = answers.pop()
  check(typeof selected === 'object', 'Group must send a structured answer')
  check(selected.question_revision === 7 && selected.selections.length === 2, 'Bind question revision')
  check(selected.selections[1].field_id === 'source-1' && selected.selections[1].choice_id === 'current'
    && selected.selections[1].leg_id === 'leg-1' && selected.selections[1].field === 'source', 'Bind field and leg')
  await api.answer('synthetic-conversation', question.turn_id, selected)
  check(requests[0].answers_question_id === question.turn_id && !('text' in requests[0]), 'API sends IDs without fabricated text')
  passed.push('Grouped fields wait for all choices and send question/field/leg/revision bindings')

  await render({ ...question, turn_id: 'question-2' })
  check(button('Use these choices').disabled, 'New question must clear previous selections')
  check(document.activeElement?.id === 'clarify-heading', 'Question heading must receive focus')
  passed.push('New question clears choices and receives focus')

  await act(async () => button('None of these').click())
  const none = answers.pop()
  check(typeof none === 'string', 'None-of-these must use free-text fallback')
  await api.answer('synthetic-conversation', 'question-2', none)
  check(requests[1].text === none && !('selections' in requests[1]), 'Free-text API must preserve words')
  passed.push('None-of-these uses extraction fallback')

  // Identical labels must retain the actual selected choice ID, never find by label.
  await render({ turn_id: 'duplicate-labels', text: 'Who?', clarification: { revision: 8, fields: [
    { field_id: 'target-2', leg_id: 'leg-2', field: 'target', prompt: 'Who?', choices: [
      { choice_id: 'first', label: 'John' }, { choice_id: 'second', label: 'John' },
    ] },
  ] } })
  const johns = [...host.querySelectorAll('button')].filter((b) => b.textContent === 'John')
  await act(async () => johns[1].click())
  const john = answers.pop()
  check(typeof john === 'object' && john.selections[0].choice_id === 'second', 'Single choice must use ID')
  passed.push('Single choices submit the clicked ID, even for identical labels')

  await render(question, true)
  check([...host.querySelectorAll('button')].every((b) => b.disabled), 'Busy card must prevent submissions')
  passed.push('Busy card disables choices and submissions')

  await render({ turn_id: 'legacy', text: 'Which account: Savings or Current?' })
  await act(async () => button('Savings').click())
  check(answers.pop() === 'Savings', 'Legacy questions must still allow text answers')
  passed.push('Legacy option chips remain compatible')

  results.textContent = `${passed.length} PASS\n${passed.join('\n')}`
}

runTests().catch((error) => { results.textContent = `FAIL: ${String(error)}` }).finally(async () => {
  await act(async () => root.unmount())
})

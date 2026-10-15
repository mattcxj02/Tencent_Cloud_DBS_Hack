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
const PAYEES = [{ payee_id: 'payee-ahma', display_name: 'Ah Ma', bank: 'POSB', masked_suffix: '7766',
  kind: 'bank', aliases: [], nicknames: [] }]
window.fetch = async (_input, init) => {
  if (!init?.body) {  // GET /payees, for the nickname offer
    return new Response(JSON.stringify(PAYEES), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  requests.push(JSON.parse(String(init.body)))
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
  check([...host.querySelectorAll('legend')].map((l) => l.textContent).join('|') === 'Recipient|Pay from',
    'One payment must not number its choice groups')
  check(button('Use these choices').disabled, 'Incomplete group must not submit')
  await act(async () => button('Mum (account ending 9988)').click())
  check(host.querySelector('[aria-pressed="true"]')?.textContent === '✓Mum (account ending 9988)', 'Selected choice must be marked')
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

  await render({ turn_id: 'grouped-text', text: 'Payment 1: Who should I pay?\nPayment 1: Which account: Savings or Current?' })
  check(!host.querySelector('[aria-label="Choose an answer"]'), 'Grouped text must not offer chips for its last line only')
  passed.push('Grouped free-text questions offer no partial chips')

  await render({ turn_id: 'balances', text: 'Which account?', clarification: { revision: 9, fields: [
    { field_id: 'source-3', leg_id: 'leg-3', field: 'source', prompt: 'Which account?', amount: '3000.00', choices: [
      { choice_id: 'joint', label: 'joint (account ending 9012)', available: '2000.00', insufficient: true },
      { choice_id: 'current', label: 'current (account ending 5678)', available: '3500.00' },
    ] },
  ] } })
  const [short, enough] = [...host.querySelectorAll<HTMLButtonElement>('[aria-label="Choose an answer"] button')]
  // Advisory only: an earlier payment in the same request may top the account up.
  check(!short.disabled && short.textContent?.includes('SGD 2,000.00 available, may not cover this payment'),
    'An account that may not cover the amount must say so but stay selectable')
  check(!enough.disabled && enough.textContent?.includes('SGD 3,500.00 available'), 'Balance must show')
  await act(async () => enough.click())
  const balanceAnswer = answers.pop()
  check(typeof balanceAnswer === 'object' && balanceAnswer.selections[0].choice_id === 'current',
    'Balance details must not change the submitted choice')
  passed.push('Account choices show balances and flag short accounts')

  await render({ turn_id: 'not-found-2',
    text: "I couldn't find “Joy” among your saved payees, billers or accounts. Who should I pay? I can only pay saved payees or your own accounts.",
    clarification: { revision: 10, fields: [
      { field_id: 'target-4', leg_id: 'leg-1', field: 'target', prompt: 'Who?', choices: [
        { choice_id: 'ahma', label: 'Ah Ma (account ending 7766)' },
        { choice_id: 'savings', label: 'savings (account ending 1234)' },
      ] },
    ] } })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  check(host.textContent?.includes('Also remember “Joy” as a nickname for the payee I pick (saved payees only)'),
    'A not-found question must offer to remember the words')
  passed.push('Not-found questions offer to remember a nickname')

  results.textContent = `${passed.length} PASS\n${passed.join('\n')}`
}

runTests().catch((error) => { results.textContent = `FAIL: ${String(error)}` }).finally(async () => {
  await act(async () => root.unmount())
})

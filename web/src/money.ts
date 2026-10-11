// Money arrives from the gateway as two-decimal strings ("50.00"). All arithmetic here
// is done in integer cents; floats never touch an amount.

const MONEY_RE = /^(-)?(\d{1,13})(?:\.(\d{1,2}))?$/

/** Parse a decimal money string into integer cents. Throws on anything else. */
export function toCents(amount: string): number {
  const m = MONEY_RE.exec(amount.trim())
  if (!m) throw new Error(`invalid amount: ${amount}`)
  const cents = Number(m[2]) * 100 + Number((m[3] ?? '').padEnd(2, '0'))
  return m[1] ? -cents : cents
}

/** True for a positive amount with at most two decimals (what users may type). */
export function isPositiveMoney(text: string): boolean {
  const m = MONEY_RE.exec(text.trim())
  return m !== null && !m[1] && toCents(text) > 0
}

/** Integer cents → "1234.50". */
export function centsToString(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

function groupThousands(whole: string): string {
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Integer cents → "1,234.50" (no currency). */
export function formatCents(cents: number): string {
  const [whole, frac] = centsToString(cents).split('.')
  return `${groupThousands(whole)}.${frac}`
}

/** "1234.5" → "SGD 1,234.50". Unparseable input is shown verbatim, never guessed at. */
export function formatMoney(amount: string, currency = 'SGD'): string {
  try {
    return `${currency} ${formatCents(toCents(amount))}`
  } catch {
    return `${currency} ${amount}`
  }
}

/** Sum two-decimal strings in integer cents. */
export function sumCents(amounts: string[]): number {
  return amounts.reduce((total, a) => total + toCents(a), 0)
}

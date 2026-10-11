// Small shared building blocks. Every state is conveyed by words (and a symbol),
// never by colour alone; every control is at least 44 px tall.
import { useEffect, useRef } from 'react'
import type { ComponentPropsWithRef, ReactNode, Ref } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'approve'

const VARIANT: Record<Variant, string> = {
  primary: 'bg-indigo-700 text-white hover:bg-indigo-800 border border-indigo-700',
  approve: 'bg-green-800 text-white hover:bg-green-900 border border-green-800',
  secondary: 'bg-white text-slate-900 hover:bg-slate-100 border border-slate-400',
  danger: 'bg-white text-red-800 hover:bg-red-50 border border-red-700',
  ghost: 'bg-transparent text-indigo-800 hover:bg-indigo-50 border border-transparent underline-offset-2 hover:underline',
}

// React 19: `ref` is an ordinary prop, passed through to the <button>.
interface ButtonProps extends ComponentPropsWithRef<'button'> {
  variant?: Variant
  busy?: boolean
  busyLabel?: string
  full?: boolean
}

export function Button({
  variant = 'primary',
  busy = false,
  busyLabel,
  full = false,
  className = '',
  disabled,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      {...rest}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex min-h-[44px] items-center justify-center gap-2 rounded-lg px-4 py-2 text-base font-semibold disabled:cursor-not-allowed disabled:opacity-60 ${
        full ? 'w-full' : ''
      } ${VARIANT[variant]} ${className}`}
    >
      {busy && (
        <span
          aria-hidden="true"
          className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {busy && busyLabel ? busyLabel : children}
    </button>
  )
}

export function Card({
  title,
  titleId,
  children,
  className = '',
  headingRef,
  actions,
}: {
  title: ReactNode
  titleId: string
  children: ReactNode
  className?: string
  headingRef?: Ref<HTMLHeadingElement>
  actions?: ReactNode
}) {
  return (
    <section aria-labelledby={titleId} className={`rounded-xl border border-slate-300 bg-white p-4 shadow-sm ${className}`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <h2 id={titleId} ref={headingRef} tabIndex={-1} className="text-lg font-bold text-slate-900">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  )
}

type NoticeKind = 'info' | 'success' | 'warning' | 'error'

const NOTICE: Record<NoticeKind, { cls: string; symbol: string; label: string }> = {
  info: { cls: 'border-sky-700 bg-sky-50 text-sky-950', symbol: 'ℹ', label: 'Note' },
  success: { cls: 'border-green-700 bg-green-50 text-green-950', symbol: '✓', label: 'Done' },
  warning: { cls: 'border-amber-600 bg-amber-50 text-amber-950', symbol: '!', label: 'Check' },
  error: { cls: 'border-red-700 bg-red-50 text-red-950', symbol: '✕', label: 'Problem' },
}

/**
 * A boxed message with a text label ("Problem:", "Done:"...). Errors are role="alert"
 * and take focus when `focus` is set, so keyboard and screen-reader users land on them.
 */
export function Notice({
  kind,
  title,
  children,
  focus = false,
  label,
}: {
  kind: NoticeKind
  title?: ReactNode
  children?: ReactNode
  focus?: boolean
  label?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  // Focus on mount and when the (string) message changes, never on unrelated re-renders
  // (e.g. a countdown tick), so keyboard users are not pulled back to the message.
  const messageKey = typeof title === 'string' ? title : null
  useEffect(() => {
    if (focus) ref.current?.focus()
  }, [focus, messageKey])
  const n = NOTICE[kind]
  return (
    <div
      ref={ref}
      tabIndex={focus ? -1 : undefined}
      role={kind === 'error' ? 'alert' : undefined}
      className={`rounded-lg border-l-4 p-3 text-base ${n.cls}`}
    >
      <p className="font-semibold">
        <span aria-hidden="true" className="mr-1 inline-block w-4 text-center">
          {n.symbol}
        </span>
        {label ?? n.label}
        {title ? <>: {title}</> : null}
      </p>
      {children && <div className="mt-1 space-y-2">{children}</div>}
    </div>
  )
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'changed' }) {
  const cls = {
    neutral: 'border-slate-500 text-slate-800 bg-slate-50',
    good: 'border-green-700 text-green-900 bg-green-50',
    warn: 'border-amber-700 text-amber-950 bg-amber-50',
    bad: 'border-red-700 text-red-900 bg-red-50',
    changed: 'border-indigo-700 text-indigo-950 bg-indigo-50',
  }[tone]
  return <span className={`inline-block rounded-full border px-2 py-0.5 text-sm font-semibold ${cls}`}>{children}</span>
}

/** Page title (h1) that receives focus when the page opens, for keyboard and screen readers. */
export function PageTitle({ children, sub }: { children: ReactNode; sub?: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    ref.current?.focus()
  }, [])
  return (
    <div className="mb-4">
      <h1 ref={ref} tabIndex={-1} className="text-2xl font-bold text-slate-900">
        {children}
      </h1>
      {sub && <p className="mt-1 text-base text-slate-700">{sub}</p>}
    </div>
  )
}

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-base font-semibold text-slate-900">
        {label}
      </label>
      {hint && (
        <p id={`${id}-hint`} className="text-sm text-slate-700">
          {hint}
        </p>
      )}
      {children}
    </div>
  )
}

export const inputClass =
  'block w-full min-h-[44px] rounded-lg border border-slate-500 bg-white px-3 py-2 text-base text-slate-900 placeholder:text-slate-500'

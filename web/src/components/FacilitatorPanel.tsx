// Usability-session helper for the facilitator (T-14): task timer and control-activation
// count, kept in this page only (nothing is sent anywhere). Stops automatically when a
// task completes (a receipt appears) and keeps a list of runs to copy into notes.
import { useCallback, useEffect, useRef, useState } from 'react'

const FLAG = 'dcta.facilitator'
const TOGGLE_EVENT = 'dcta:facilitator-toggle'
export const TASK_COMPLETE_EVENT = 'dcta:task-complete'

function readFlag(): boolean {
  try {
    return window.localStorage.getItem(FLAG) === '1'
  } catch {
    return false
  }
}

export function setFacilitatorEnabled(on: boolean): void {
  try {
    if (on) window.localStorage.setItem(FLAG, '1')
    else window.localStorage.removeItem(FLAG)
  } catch {
    // storage blocked: the toggle lasts for this page view only
  }
  window.dispatchEvent(new CustomEvent(TOGGLE_EVENT, { detail: on }))
}

export function useFacilitatorEnabled(): boolean {
  const [on, setOn] = useState(readFlag)
  useEffect(() => {
    const handler = (e: Event) => setOn(Boolean((e as CustomEvent<boolean>).detail))
    window.addEventListener(TOGGLE_EVENT, handler)
    return () => window.removeEventListener(TOGGLE_EVENT, handler)
  }, [])
  return on
}

interface Run {
  n: number
  seconds: number
  clicks: number
  completed: boolean
}

const CONTROL = 'button, a, input, select, textarea, summary, [role="button"], label'

export function FacilitatorPanel({ scope }: { scope: string }) {
  const [running, setRunning] = useState(false)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [endedAt, setEndedAt] = useState<number | null>(null)
  const [now, setNow] = useState(Date.now())
  const [clicks, setClicks] = useState(0)
  const [runs, setRuns] = useState<Run[]>([])
  const panelRef = useRef<HTMLElement>(null)
  const state = useRef({ running: false, startedAt: 0, clicks: 0 })

  const finish = useCallback((completed: boolean) => {
    const s = state.current
    if (!s.running) return
    s.running = false
    const end = Date.now()
    setRunning(false)
    setEndedAt(end)
    const seconds = Math.round((end - s.startedAt) / 100) / 10
    setRuns((r) => [...r, { n: r.length + 1, seconds, clicks: s.clicks, completed }])
  }, [])

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const s = state.current
      const target = e.target as Element | null
      if (!s.running || !target?.closest(CONTROL)) return
      if (panelRef.current?.contains(target)) return
      s.clicks += 1
      setClicks(s.clicks)
    }
    const onDone = () => finish(true)
    document.addEventListener('click', onClick, true)
    window.addEventListener(TASK_COMPLETE_EVENT, onDone)
    return () => {
      document.removeEventListener('click', onClick, true)
      window.removeEventListener(TASK_COMPLETE_EVENT, onDone)
    }
  }, [finish])

  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(id)
  }, [running])

  const start = () => {
    const t = Date.now()
    state.current = { running: true, startedAt: t, clicks: 0 }
    setStartedAt(t)
    setEndedAt(null)
    setNow(t)
    setClicks(0)
    setRunning(true)
  }

  const elapsed = startedAt === null ? 0 : ((running ? now : (endedAt ?? startedAt)) - startedAt) / 1000

  return (
    <aside
      ref={panelRef}
      aria-labelledby="facilitator-heading"
      className="rounded-xl border-2 border-dashed border-slate-500 bg-slate-50 p-3 text-sm"
    >
      <h2 id="facilitator-heading" className="text-base font-bold">
        Facilitator: task timer ({scope})
      </h2>
      <p className="text-slate-700">
        Counts clicks or keyboard activations of controls (not typing, not this panel). Stops by itself when an assistant payment completes; otherwise press “Mark completed”.
      </p>
      <p className="mt-1 text-lg tabular-nums" aria-live="off">
        Time <strong>{elapsed.toFixed(1)} s</strong> · Clicks <strong>{clicks}</strong> ·{' '}
        {running ? 'Running' : 'Stopped'}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={start}
          className="min-h-[44px] rounded-lg border border-slate-500 bg-white px-3 font-semibold"
        >
          {running ? 'Restart task' : 'Start task'}
        </button>
        <button
          type="button"
          onClick={() => finish(true)}
          disabled={!running}
          className="min-h-[44px] rounded-lg border border-slate-500 bg-white px-3 font-semibold disabled:opacity-60"
        >
          Mark completed
        </button>
        <button
          type="button"
          onClick={() => finish(false)}
          disabled={!running}
          className="min-h-[44px] rounded-lg border border-slate-500 bg-white px-3 font-semibold disabled:opacity-60"
        >
          Stop (not completed)
        </button>
      </div>
      {runs.length > 0 && (
        <table className="mt-2 w-full text-left">
          <caption className="sr-only">Recorded runs</caption>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Seconds</th>
              <th scope="col">Clicks</th>
              <th scope="col">Completed</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.n}>
                <td>{r.n}</td>
                <td className="tabular-nums">{r.seconds.toFixed(1)}</td>
                <td className="tabular-nums">{r.clicks}</td>
                <td>{r.completed ? 'Yes' : 'No'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </aside>
  )
}
